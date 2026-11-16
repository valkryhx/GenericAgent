import test from 'node:test'
import assert from 'node:assert/strict'
import stringWidth from 'string-width'
import {
  assistantDisplayText,
  clampTranscriptScrollOffset,
  liveTranscriptViewportLines,
  tailLines,
  transcriptLines,
  transcriptLinesWithChannel,
  visibleMessages,
  visibleMessagesForViewport,
  visibleTranscriptLines,
  wrapTranscriptLines,
} from './messageWindow.js'
import type { ChatMessage } from './protocol.js'
import { getInkTheme } from './theme.js'
import { GLYPHS, RESULT_PREFIX } from './glyphs.js'

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

test('transcriptLines renders [Action]/[Status] as CC-style result lines, equally muted', () => {
  // GA tool progress markers become CC's `  ⎿  <body>` result lines (dim).
  // Action and Status must share the same prefix + style, even when stream-commit
  // placed them on different segment lines.
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
  const rows = transcriptLines(messages, { expandedTools: false, theme: getInkTheme('default') })
  const action = rows.find(row => row.text.includes('Calling MCP tool'))
  const status = rows.find(row => row.text.includes('MCP success'))
  assert.ok(action)
  assert.ok(status)
  assert.equal(action!.text, `${RESULT_PREFIX}Calling MCP tool: mcp__tavily__tavily_search`)
  assert.equal(status!.text, `${RESULT_PREFIX}MCP success`)
  assert.equal(action!.parts?.[0]?.color, 'gray')
  assert.equal(status!.parts?.[0]?.color, 'gray')
  assert.equal(action!.parts?.[0]?.dimColor, true)
  assert.equal(status!.parts?.[0]?.dimColor, true)
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
  const rows = transcriptLines(messages, { expandedTools: false, theme: getInkTheme('default') })
  const action = rows.find(row => row.text.includes('Calling MCP tool'))
  const status = rows.find(row => row.text.includes('MCP success'))
  assert.ok(action)
  assert.ok(status)
  assert.equal(action!.text, `${RESULT_PREFIX}Calling MCP tool: x`)
  assert.equal(status!.text, `${RESULT_PREFIX}MCP success`)
  assert.equal(action!.parts?.[0]?.color, 'gray')
  assert.equal(status!.parts?.[0]?.color, 'gray')
})

test('transcriptLines forces [Action]/[Status] to muted gray even when one side was a code fence', () => {
  // Historical bug: open fence -> Action rendered as theme.code (cyan); Status outside
  // fence -> default foreground. Users saw "calling=blue, success=black".
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
  const rows = transcriptLines(messages, { expandedTools: false, theme })
  const action = rows.find(row => row.text.includes('Calling MCP tool'))
  const status = rows.find(row => row.text.includes('MCP success'))
  assert.ok(action)
  assert.ok(status)
  const actionColor = action!.parts?.[0]?.color
  const statusColor = status!.parts?.[0]?.color
  assert.equal(actionColor, 'gray')
  assert.equal(statusColor, 'gray')
  assert.notEqual(actionColor, theme.code)
  assert.notEqual(statusColor, theme.code)
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


test('transcriptLines repairs legacy inline workflow fence close', () => {
  // Real 2026-10-12 regression: the workflow tool result had its closing
  // 5-backtick fence appended to the [Workflow] line. A stream-commit segment
  // started at the opening fence, so Turn 3 and Turn 4 were swallowed by the
  // code block and rendered as literal ** markers.
  const messages: ChatMessage[] = [{
    id: 'a-11-c12',
    role: 'assistant',
    text: [
      '`````',
      '[Workflow] launched model-availability-research (wf_test); use /workflows to monitor.`````',
      '',
      '**LLM Running (Step 3) ...**',
      '',
      '🔨 Tool: `update_working_checkpoint`  📥 args:',
      '````text',
    ].join('\n'),
    done: true,
    taskId: 11,
  }]

  const rows = transcriptLines(messages, { expandedTools: false })
  const turn = rows.find(row => row.text.includes('LLM Running (Step 3)'))
  assert.ok(turn)
  assert.equal(turn!.text, 'LLM Running (Step 3) ...')
  assert.equal(turn!.parts?.some(part => part.bold), true)
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

test('transcriptLines keeps [Stdout] body dim and does not emit an empty result marker', () => {
  const messages: ChatMessage[] = [{
    id: 'a-stdout',
    role: 'assistant',
    text: [
      '```',
      '[Status] Exit Code: 0',
      '[Stdout]',
      '60e4340 feat(packaging): ship installable GA wheel',
      '[omitted long output]',
      'dc07912 feat(hooks): implement Claude Code compatible hook runtime',
      '```',
    ].join('\n'),
    done: true,
    taskId: 1,
  }]
  const theme = getInkTheme('default')
  const rows = transcriptLines(messages, { expandedTools: false, theme })
  const texts = rows.map(row => row.text)
  assert.equal(texts.some(text => text.trim() === '⎿'), false, 'empty [Stdout] must not leave an isolated ⎿')
  const stdout = rows.find(row => row.text.includes('60e4340'))
  const omitted = rows.find(row => row.text.includes('[omitted long output]'))
  const later = rows.find(row => row.text.includes('dc07912'))
  assert.ok(stdout && omitted && later)
  for (const row of [stdout!, omitted!, later!]) {
    assert.equal(row.parts?.[0]?.color, 'gray')
    assert.equal(row.parts?.[0]?.dimColor, true)
  }
})

test('transcriptLines folds an open streaming tool args fence to one row', () => {
  const partial = [
    '先跑一下。',
    '⏺ Tool: `code_run` args:',
    '````text',
    '{',
    ...Array.from({ length: 50 }, (_, index) => `  "line_${index}": ${index},`),
  ].join('\n')
  const rows = transcriptLines([
    { id: 'a-stream', role: 'assistant', text: partial, done: false, taskId: 1 },
  ], { expandedTools: false })

  assert.deepEqual(rows.map(row => row.text).filter(text => text.trim()), [
    '先跑一下。',
    `${GLYPHS.bullet} code_run`,
  ])
})

test('transcriptLinesWithChannel carries stdout state from Static into live', () => {
  const staticPart = transcriptLinesWithChannel([
    { id: 'a-static', role: 'assistant', text: ['[Status] Exit Code: 0', '[Stdout]'].join(String.fromCharCode(10)), done: true, taskId: 3 },
  ], { expandedTools: false })
  const livePart = transcriptLinesWithChannel([
    { id: 'a-live', role: 'assistant', text: 'stdout body in live', done: false, taskId: 4 },
  ], { expandedTools: false, initialChannel: staticPart.channel })
  const texts = livePart.lines.map(row => row.text)

  assert.deepEqual(texts.filter(text => text.trim()), [`${RESULT_PREFIX}stdout body in live`])
  assert.equal(livePart.lines[0]?.parts?.[0]?.dimColor, true)
})

test('transcriptLines keeps a stdout block real across a stream-commit split', () => {
  const messages: ChatMessage[] = [
    {
      id: 'a-split-c0',
      role: 'assistant',
      text: ['[Status] Exit Code: 0', '[Stdout]'].join('\n'),
      done: true,
      taskId: 7,
    },
    {
      id: 'a-split-c1',
      role: 'assistant',
      text: ['first stdout line', 'second stdout line'].join('\n'),
      done: true,
      taskId: 7,
    },
  ]
  const theme = getInkTheme('default')
  const rows = transcriptLines(messages, { expandedTools: false, theme })
  const texts = rows.map(row => row.text)

  assert.deepEqual(texts.filter(text => text.trim()), [
    `${RESULT_PREFIX}Exit Code: 0`,
    `${RESULT_PREFIX}first stdout line`,
    `      second stdout line`,
  ])
  const body = rows.find(row => row.text.includes('first stdout line'))
  assert.equal(body?.parts?.[0]?.color, 'gray')
  assert.equal(body?.parts?.[0]?.dimColor, true)
})

test('transcriptLines exits stdout mode at an orphan closing fence in a later segment', () => {
  const messages: ChatMessage[] = [
    { id: 'a-orphan-c0', role: 'assistant', text: ['[Stdout]', 'command output'].join(String.fromCharCode(10)), done: true, taskId: 11 },
    { id: 'a-orphan-c1', role: 'assistant', text: ['```', '', 'final answer'].join(String.fromCharCode(10)), done: true, taskId: 11 },
  ]
  const rows = transcriptLines(messages, { expandedTools: false })
  const final = rows.find(row => row.text === 'final answer')

  assert.ok(final)
  assert.equal(final!.parts?.[0]?.dimColor, undefined)
  assert.equal(rows.some(row => row.text.trim() === '```'), false)
})

test('transcriptLines exits stdout mode at the closing fence sentinel', () => {
  const messages: ChatMessage[] = [{
    id: 'a-stdout-close',
    role: 'assistant',
    text: [
      '`````',
      '[Stdout]',
      'command output',
      '`````',
      '',
      'final answer',
    ].join('\n'),
    done: true,
    taskId: 9,
  }]
  const rows = transcriptLines(messages, { expandedTools: false })
  const final = rows.find(row => row.text === 'final answer')

  assert.ok(final)
  assert.equal(final!.parts?.[0]?.dimColor, undefined)
  assert.ok(rows.some(row => row.text === `${RESULT_PREFIX}command output`))
})

test('transcriptLines exits stdout mode at the next real step header', () => {
  const messages: ChatMessage[] = [{
    id: 'a-stdout-turn2',
    role: 'assistant',
    text: [
      '[Stdout]',
      'command output',
      '',
      '**LLM Running (Step 2) ...**',
      '',
      'second step answer',
    ].join('\n'),
    done: true,
    taskId: 8,
  }]
  const rows = transcriptLines(messages, { expandedTools: false })
  const step = rows.find(row => row.text.includes('LLM Running (Step 2)'))

  assert.ok(step)
  assert.equal(step!.text, 'LLM Running (Step 2) ...')
  assert.equal(step!.parts?.[0]?.color, undefined)
  assert.ok(rows.some(row => row.text === 'second step answer'))
})

test('transcriptLines keeps consecutive status rows contiguous like CC tool results', () => {
  const messages: ChatMessage[] = [{
    id: 'a-status-group',
    role: 'assistant',
    text: [
      '● file_read({path: x})',
      '[Status] Reading file: a',
      '',
      '[Status] Reading file: b',
      '',
      '[Status] Reading file: c',
    ].join('\n'),
    done: true,
    taskId: 10,
  }]
  const rows = transcriptLines(messages, { expandedTools: false })
  const texts = rows.map(row => row.text).filter(text => text.trim())

  assert.deepEqual(texts, [
    '● file_read({path: x})',
    `${RESULT_PREFIX}Reading file: a`,
    `${RESULT_PREFIX}Reading file: b`,
    `${RESULT_PREFIX}Reading file: c`,
  ])
})

test('transcriptLines keeps the rendered prefix append-only across stream-commit segments', () => {
  // Ink <Static> appends by item count. Every segment that has already been
  // committed must remain a byte-for-byte prefix of the next render; otherwise
  // Static skips the first row of the newly committed segment. This was the
  // long-stdout "one missing line per commit" bug (L006/L022/L037/...).
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: '跑大量输出', done: true, taskId: 1 },
    { id: 'a-1-c0', role: 'assistant', text: '我先执行一个脚本。', done: true, taskId: 1 },
    { id: 'a-1-c1', role: 'assistant', text: '[Status] Exit Code: 0', done: true, taskId: 1 },
    { id: 'a-1-c2', role: 'assistant', text: '[Stdout]\n\nL000 stdout payload ........\nL001 stdout payload ........\nL002 stdout payload ........', done: true, taskId: 1 },
    { id: 'a-1-c3', role: 'assistant', text: 'L003 stdout payload ........\nL004 stdout payload ........\nL005 stdout payload ........', done: true, taskId: 1 },
    { id: 'a-1-c4', role: 'assistant', text: 'L006 stdout payload ........\nL007 stdout payload ........\nL008 stdout payload ........', done: true, taskId: 1 },
    { id: 'a-1-c5', role: 'assistant', text: 'L009 stdout payload ........\nL010 stdout payload ........\nL011 stdout payload ........', done: true, taskId: 1 },
  ]

  let previous: string[] = []
  for (let count = 1; count <= messages.length; count++) {
    const current = transcriptLines(messages.slice(0, count), { expandedTools: false }).map(line => line.text)
    assert.deepEqual(current.slice(0, previous.length), previous,
      `render of ${count} messages changed an already-committed prefix`)
    previous = current
  }

  const all = previous.filter(text => text.trim())
  for (let index = 0; index < 12; index++) {
    const tag = `L${String(index).padStart(3, '0')} stdout payload`
    assert.equal(all.filter(text => text.includes(tag)).length, 1, `${tag} must render exactly once`)
  }
})

test('result boundary blank is appended at the next block, not removed from the previous one', () => {
  const base: ChatMessage[] = [
    { id: 'a-1-c0', role: 'assistant', text: '[Stdout]\nL001\nL002', done: true, taskId: 1 },
  ]
  const before = transcriptLines(base, { expandedTools: false }).map(line => line.text)
  const after = transcriptLines([
    ...base,
    { id: 'u-2', role: 'user', text: 'next question', done: true, taskId: 2 },
  ], { expandedTools: false }).map(line => line.text)

  assert.deepEqual(after.slice(0, before.length), before)
  // The boundary blank belongs before the prompt; the prompt itself always
  // owns a stable trailing blank (it can never be continued by a commit).
  assert.deepEqual(after.slice(before.length), [' ', '> next question', ' '])
})

test('consecutive stdout commit segments stay contiguous without phantom blanks', () => {
  const rows = transcriptLines([
    { id: 'a-1-c0', role: 'assistant', text: '[Stdout]\nL001', done: true, taskId: 1 },
    { id: 'a-1-c1', role: 'assistant', text: 'L002\nL003', done: true, taskId: 1 },
  ], { expandedTools: false }).map(line => line.text)

  assert.deepEqual(rows.filter(text => text.trim()), [
    `${RESULT_PREFIX}L001`,
    '      L002',
    '      L003',
  ])
})

test('a tool chip keeps its result tight across a stream-commit split', () => {
  // The reported symptom: identical tool calls rendered with and without a blank
  // line between `● Write(...)` and its `⎿` output, depending on whether the block
  // happened to overflow the 8-line live tail and get committed as its own segment.
  //
  // The chip message used to take a trailing blank (`appendMessageLines`), and the
  // message boundary added another, so a split inside one tool call opened a gap that
  // the same call never had when it stayed in one segment.
  const split = transcriptLines([
    { id: 'a-1-c0', role: 'assistant', text: `${GLYPHS.bullet} Write(_rev_audit.py)`, done: true, taskId: 1 },
    { id: 'a-1-c1', role: 'assistant', text: '[Action] Overwriting file: _rev_audit.py', done: true, taskId: 1 },
  ], { expandedTools: false }).map(row => row.text)

  assert.deepEqual(split, [
    `${GLYPHS.bullet} Write(_rev_audit.py)`,
    `${RESULT_PREFIX}Overwriting file: _rev_audit.py`,
  ])
})

test('a split tool group renders exactly like the unsplit one', () => {
  // The invariant behind the fix: splitting must be invisible. Every shape a tool
  // group takes (chip+result, chip+chip, result+chip) is asserted against the same
  // content in a single message rather than against a hand-written expectation, so
  // the two renderings cannot drift apart again.
  const cases: Array<{ name: string; segments: string[]; whole: string }> = [
    {
      name: 'chip + its result',
      segments: [`${GLYPHS.bullet} Write(a.py)`, '[Action] Overwriting file: a.py'],
      whole: `${GLYPHS.bullet} Write(a.py)\n[Action] Overwriting file: a.py`,
    },
    {
      name: 'chip + chip (parallel batch)',
      segments: [`${GLYPHS.bullet} code_run(x)`, `${GLYPHS.bullet} code_run(y)`],
      whole: `${GLYPHS.bullet} code_run(x)\n${GLYPHS.bullet} code_run(y)`,
    },
    {
      name: 'chip + result + chip',
      segments: [`${GLYPHS.bullet} code_run(x)\n[Action] A`, `${GLYPHS.bullet} code_run(y)`],
      whole: `${GLYPHS.bullet} code_run(x)\n[Action] A\n${GLYPHS.bullet} code_run(y)`,
    },
    {
      name: 'result + chip',
      segments: ['[Action] A', `${GLYPHS.bullet} code_run(y)`],
      whole: '[Action] A\n' + `${GLYPHS.bullet} code_run(y)`,
    },
  ]

  for (const item of cases) {
    const splitRows = transcriptLines(
      item.segments.map((text, index) => ({ id: `a-1-c${index}`, role: 'assistant' as const, text, done: true, taskId: 1 })),
      { expandedTools: false },
    ).map(row => row.text)
    const wholeRows = transcriptLines(
      [{ id: 'a-1', role: 'assistant' as const, text: item.whole, done: true, taskId: 1 }],
      { expandedTools: false },
    ).map(row => row.text)
    assert.deepEqual(splitRows, wholeRows, `${item.name}: the commit split changed the rendering`)
  }
})

test('an explicit blank line between tool groups survives the split', () => {
  // The fix must only remove blanks the split invented. GA writes an explicit blank
  // between separate result lines, and that one has to stay -- in both renderings.
  const whole = `[Action] A\n\n[Status] B`
  const splitRows = transcriptLines([
    { id: 'a-1-c0', role: 'assistant', text: '[Action] A', done: true, taskId: 1 },
    { id: 'a-1-c1', role: 'assistant', text: '[Status] B', done: true, taskId: 1 },
  ], { expandedTools: false }).map(row => row.text)
  const wholeRows = transcriptLines(
    [{ id: 'a-1', role: 'assistant', text: whole, done: true, taskId: 1 }],
    { expandedTools: false },
  ).map(row => row.text)

  assert.deepEqual(splitRows, wholeRows, 'the split invented or removed a blank line')
})

test('prose is still separated from a following tool chip', () => {
  // The other half of the rule: a chip after prose is a new block, not a continuation.
  const rows = transcriptLines([
    { id: 'a-1-c0', role: 'assistant', text: 'Here is what I found.', done: true, taskId: 1 },
    { id: 'a-1-c1', role: 'assistant', text: `${GLYPHS.bullet} Write(a.py)`, done: true, taskId: 1 },
  ], { expandedTools: false }).map(row => row.text)

  assert.equal(rows[0], 'Here is what I found.')
  assert.equal(rows[1]!.trim(), '', 'prose and a tool call were glued together')
  assert.equal(rows[2], `${GLYPHS.bullet} Write(a.py)`)
})

// ── 结构化工具调用（resume 路径）────────────────────────────────────────────
//
// 回归：`/resume` 加载历史时曾只显示纯文本，工具调用、MCP 调用及其输出全部消失。
// 原因是 `transcripts.py:_message_text_from_blocks` 只收 `type == "text"` 块，
// 而 `bridge.py` 又只把 `assistant_text` 发给前端。数据一直在 v2 transcript 里
// （`tool_use` / `tool_result` 块带完整 input 与 content），只是没走到 UI。
//
// 这组测试钉住「结构化工具 → 与实时一致的 chip / 结果行」这条不变量。

test('resume: structured toolUses render as the same chip a live turn produces', () => {
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '两个子代理已启动。',
    done: true,
    taskId: 1,
    toolUses: [{ id: 'call-a', tool: 'spawn_agent', input: JSON.stringify({ task_name: 'probe_a' }) }],
  }], { expandedTools: false }).map(row => row.text)

  assert.equal(rows[0], '两个子代理已启动。')
  const chip = rows.find(row => row.includes('spawn_agent'))
  assert.ok(chip, `no tool chip was rendered: ${JSON.stringify(rows)}`)
  assert.ok(chip!.trimStart().startsWith(GLYPHS.bullet), `chip must start with the bullet glyph: ${JSON.stringify(chip)}`)
  assert.match(chip!, /spawn_agent\(task_name: probe_a\)/)
})

test('resume: toolResults are paired to their call by tool_use_id and rendered under it', () => {
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    toolUses: [
      { id: 'call-a', tool: 'spawn_agent', input: '{}' },
      { id: 'call-b', tool: 'wait_agent', input: '{}' },
    ],
    toolResults: [
      // Deliberately out of order, and only one of the two has a result: pairing
      // must follow tool_use_id, not array position.
      { toolUseId: 'call-b', text: 'satisfied' },
    ],
  }], { expandedTools: false }).map(row => row.text)

  const joined = rows.join('\n')
  assert.match(joined, /spawn_agent/, `first call missing: ${JSON.stringify(rows)}`)
  assert.match(joined, /wait_agent/, `second call missing: ${JSON.stringify(rows)}`)
  // The `wait_agent` result belongs to the second chip, and `spawn_agent` got none.
  const waitIndex = rows.findIndex(row => row.includes('wait_agent'))
  const resultIndex = rows.findIndex(row => row.includes('satisfied'))
  assert.ok(resultIndex > waitIndex, `result was not attached to its own call: ${JSON.stringify(rows)}`)
  assert.equal(rows.filter(row => row.includes('satisfied')).length, 1, 'the result was duplicated onto both calls')
})

test('resume: a turn with only tool calls and no prose still renders (it is not dropped)', () => {
  // The model answered entirely through a tool; `assistant_text` is empty, so the
  // old projection emitted no assistant message at all and the turn vanished.
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    toolUses: [{ id: 'call-a', tool: 'read_agent_result', input: '{}' }],
  }], { expandedTools: false }).map(row => row.text)

  assert.ok(rows.some(row => row.includes('read_agent_result')), `turn was dropped: ${JSON.stringify(rows)}`)
})

test('resume: a message without toolUses renders exactly as before (no regression)', () => {
  const rows = transcriptLines([
    { id: 'u-1', role: 'user', text: 'hello', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: 'hi', done: true, taskId: 1 },
  ], { expandedTools: false }).map(row => row.text)

  assert.equal(rows[0], '> hello')
  // The user block is followed by its own blank separator row, then the assistant.
  assert.equal(rows.find(row => row === 'hi'), 'hi', `assistant line missing: ${JSON.stringify(rows)}`)
  assert.ok(!rows.join('\n').includes(GLYPHS.bullet), 'a plain message grew a tool chip')
})

// ── thinking（推理过程）──────────────────────────────────────────────────
//
// 回归：transcript 里一直有 `thinking` 块，但没有任何渲染路径消费它，所以用户
// 从来没见过模型的思考过程——resume 看不到，实时也看不到。CC 319 把 thinking
// 当作独立块渲染（`m0591:214511 lio()`），GA 复用工具 chip 的折叠开关：
// 折叠时一行摘要，Ctrl+O 展开全文。

test('thinking: collapsed renders a single summary line with the star glyph', () => {
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: 'done',
    done: true,
    taskId: 1,
    thinking: ['Wait for A first.\nThen read the result.'],
  }], { expandedTools: false }).map(row => row.text)

  const summary = rows.find(row => row.includes(GLYPHS.star))
  assert.ok(summary, `no thinking summary line: ${JSON.stringify(rows)}`)
  assert.ok(summary!.includes(GLYPHS.triangleRight), `collapsed marker missing: ${JSON.stringify(summary)}`)
  // Multi-line reasoning collapses onto one line.
  assert.ok(summary!.includes('Wait for A first. Then read the result.'), `text not flattened: ${JSON.stringify(summary)}`)
  assert.equal(rows.filter(row => row.includes(GLYPHS.star)).length, 1, 'collapsed thinking must be one line')
})

test('thinking: expanded shows every line under a disclosure header', () => {
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: 'done',
    done: true,
    taskId: 1,
    thinking: ['first line\nsecond line'],
  }], { expandedTools: true }).map(row => row.text)

  const header = rows.find(row => row.includes('thinking'))
  assert.ok(header, `no thinking header: ${JSON.stringify(rows)}`)
  assert.ok(header!.includes(GLYPHS.triangleDown), `expanded marker missing: ${JSON.stringify(header)}`)
  assert.ok(rows.some(row => row.includes('first line')), `body line 1 missing: ${JSON.stringify(rows)}`)
  assert.ok(rows.some(row => row.includes('second line')), `body line 2 missing: ${JSON.stringify(rows)}`)
})

test('thinking: several blocks each get their own row', () => {
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    thinking: ['step one', 'step two'],
  }], { expandedTools: false }).map(row => row.text)

  assert.equal(rows.filter(row => row.includes(GLYPHS.star)).length, 2, `expected one row per block: ${JSON.stringify(rows)}`)
})

test('thinking: long reasoning is truncated when collapsed', () => {
  const long = 'x'.repeat(400)
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    thinking: [long],
  }], { expandedTools: false }).map(row => row.text)

  const summary = rows.find(row => row.includes(GLYPHS.star))!
  assert.ok(summary.length < 160, `collapsed summary was not truncated: ${summary.length} chars`)
  assert.ok(summary.includes('…'), 'truncation marker missing')
})

test('thinking: a message without thinking gains no thinking row', () => {
  const rows = transcriptLines([
    { id: 'a-1', role: 'assistant', text: 'plain answer', done: true, taskId: 1 },
  ], { expandedTools: false }).map(row => row.text)

  assert.ok(!rows.join('\n').includes(GLYPHS.star), `a plain message grew a thinking row: ${JSON.stringify(rows)}`)
})

// ── resume 块顺序 ─────────────────────────────────────────────────────
//
// 回归：`load_v2` 曾把同回合的块按类型压平成 thinking / toolUses / toolResults
// 三个数组。三个数组各自完整，所以「块在不在」的断言全绿 —— 但多步回合是
// `thinking → text → tool_use → tool_result` 反复交错，类型分组表达不了，
// 渲染只能 reasoning 在前、tools 在后。用户报的正是这个：实时顺序对，
// resume 后「所有 thinking 在最前，所有 toolcall 在最后」。

test('resume blocks replay the recorded order instead of grouping by type', () => {
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '先搜一下。\n再查一次。',
    done: true,
    taskId: 1,
    thinking: ['THINK-1', 'THINK-2'],
    toolUses: [
      { id: 'c1', tool: 'web_search', input: '{"q":"x"}' },
      { id: 'c2', tool: 'web_fetch', input: '{"url":"y"}' },
    ],
    toolResults: [
      { toolUseId: 'c1', text: 'RESULT-1' },
      { toolUseId: 'c2', text: 'RESULT-2' },
    ],
    blocks: [
      { kind: 'thinking', text: 'THINK-1' },
      { kind: 'text', text: '先搜一下。' },
      { kind: 'tool_use', id: 'c1', tool: 'web_search', input: '{"q":"x"}' },
      { kind: 'tool_result', toolUseId: 'c1', text: 'RESULT-1' },
      { kind: 'thinking', text: 'THINK-2' },
      { kind: 'text', text: '再查一次。' },
      { kind: 'tool_use', id: 'c2', tool: 'web_fetch', input: '{"url":"y"}' },
      { kind: 'tool_result', toolUseId: 'c2', text: 'RESULT-2' },
    ],
  }], { expandedTools: false }).map(row => row.text)

  const at = (needle: string) => rows.findIndex(row => row.includes(needle))
  // 每轮内部的相对顺序
  assert.ok(at('THINK-1') < at('先搜一下'), `THINK-1 应在第 1 轮正文之前: ${JSON.stringify(rows)}`)
  assert.ok(at('先搜一下') < at('web_search'), `第 1 轮正文应在工具之前: ${JSON.stringify(rows)}`)
  assert.ok(at('web_search') < at('THINK-2'), `THINK-2 应在第 1 轮工具之后: ${JSON.stringify(rows)}`)
  assert.ok(at('THINK-2') < at('再查一次'), `THINK-2 应在第 2 轮正文之前: ${JSON.stringify(rows)}`)
  assert.ok(at('再查一次') < at('web_fetch'), `第 2 轮正文应在工具之前: ${JSON.stringify(rows)}`)
})

test('resume blocks never render a tool result twice', () => {
  // 结果按 toolUseId 挂在 chip 下面，`tool_result` 块本身不能重复画一遍。
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: 'x',
    done: true,
    taskId: 1,
    blocks: [
      { kind: 'tool_use', id: 'c1', tool: 'code_run', input: '{"code":"1"}' },
      { kind: 'tool_result', toolUseId: 'c1', text: 'OUTPUT-ONCE' },
    ],
  }], { expandedTools: false }).map(row => row.text)

  assert.equal(rows.filter(row => row.includes('OUTPUT-ONCE')).length, 1, JSON.stringify(rows))
})

test('resume blocks still render a result whose call was dropped', () => {
  // 压缩可能丢掉 assistant 消息却留下结果；丢掉工具输出比留一行未配对更糟。
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    blocks: [
      { kind: 'tool_result', toolUseId: 'gone', text: 'ORPHAN-OUTPUT' },
    ],
  }], { expandedTools: false }).map(row => row.text)

  assert.ok(rows.some(row => row.includes('ORPHAN-OUTPUT')), JSON.stringify(rows))
})

test('a message without blocks keeps the legacy type-grouped order', () => {
  // 对照：老消息（没有顺序信息）行为必须与修复前逐字节一致。
  const rows = transcriptLines([{
    id: 'a-2',
    role: 'assistant',
    text: '正文',
    done: true,
    taskId: 2,
    thinking: ['OLD-THINK'],
    toolUses: [{ id: 'c9', tool: 'code_run', input: '{"code":"1"}' }],
    toolResults: [{ toolUseId: 'c9', text: 'OLD-RESULT' }],
  }], { expandedTools: false }).map(row => row.text)

  const at = (needle: string) => rows.findIndex(row => row.includes(needle))
  assert.ok(at('OLD-THINK') < at('正文'), JSON.stringify(rows))
  assert.ok(at('正文') < at('code_run'), JSON.stringify(rows))
  assert.ok(rows.some(row => row.includes('OLD-RESULT')), JSON.stringify(rows))
})

// ── persisted-output 信封 ─────────────────────────────────────────────
//
// 回归：`tool_results/budget.py` 把超大结果存盘后返回一个**九行**信封
// （`<persisted-output>`、大小、绝对路径、相对路径、字符数、truncated、sha256、
// `Preview:`、预览）。渲染器原本逐行铺开，两个信封就吃掉 18 行，把回合真正的
// 正文挤出屏幕 —— 用户读到的就是「同一句提示反复出现，看不到正常内容」。

const PERSISTED_ENVELOPE = [
  '<persisted-output>',
  'Output too large (74859 chars). Full output saved to: D:\repo\tool-results\call-1.json',
  'artifactPath: tool-results/session/call-1.json',
  'originalChars: 74859',
  'truncated: false',
  'sha256: ee1c8763216d894f12f78dc252134e523af9671585d4261241af701745adc036',
  'Preview:',
  '{"status":"success","_truncated":true}',
  '</persisted-output>',
].join('\n')

test('persisted-output envelope collapses to a single summary row', () => {
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    blocks: [
      { kind: 'tool_use', id: 'c1', tool: 'tavily_search', input: '{"query":"x"}' },
      { kind: 'tool_result', toolUseId: 'c1', text: PERSISTED_ENVELOPE },
    ],
  }], { expandedTools: false }).map(row => row.text)

  const resultRows = rows.filter(row => row.trim() !== '').slice(-1)
  assert.equal(resultRows.length, 1, `信封应只占一行: ${JSON.stringify(rows)}`)
  const row = resultRows[0]!
  assert.ok(row.includes('saved to file'), `没表达出「存到文件」: ${row}`)
  assert.ok(row.includes('74859'), `没带字符数: ${row}`)
  assert.ok(row.includes('tool-results/session/call-1.json'), `没带可直接使用的相对路径: ${row}`)
  // 元数据行不能漏到正文里
  assert.ok(!row.includes('sha256'), `sha256 不该占一行: ${row}`)
  assert.ok(!row.includes('<persisted-output>'), `标记不该画出来: ${row}`)
})

test('persisted-output envelope never renders the machine-shaped preview', () => {
  // 预览是 payload 的碎片（`{"status":"success","_truncated":true,...}`），
  // 对读者零信息量。
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    blocks: [
      { kind: 'tool_use', id: 'c1', tool: 'x', input: '{}' },
      { kind: 'tool_result', toolUseId: 'c1', text: PERSISTED_ENVELOPE },
    ],
  }], { expandedTools: false }).map(row => row.text)

  assert.ok(!rows.join('\n').includes('_truncated'), `JSON 预览漏出来了: ${JSON.stringify(rows)}`)
})

test('a truncated envelope says only the first portion was kept', () => {
  const envelope = PERSISTED_ENVELOPE.replace('truncated: false', 'truncated: true')
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    blocks: [
      { kind: 'tool_use', id: 'c1', tool: 'x', input: '{}' },
      { kind: 'tool_result', toolUseId: 'c1', text: envelope },
    ],
  }], { expandedTools: false }).map(row => row.text)

  const row = rows.filter(line => line.trim() !== '').slice(-1)[0]!
  assert.ok(row.includes('first portion only'), `没提示只存了前面部分: ${row}`)
})

test('an ordinary multi-line result is untouched by the envelope collapse', () => {
  // 对照：非信封结果行为必须与修复前一致。
  const rows = transcriptLines([{
    id: 'a-1',
    role: 'assistant',
    text: '',
    done: true,
    taskId: 1,
    blocks: [
      { kind: 'tool_use', id: 'c1', tool: 'x', input: '{}' },
      { kind: 'tool_result', toolUseId: 'c1', text: 'line one\nline two' },
    ],
  }], { expandedTools: false }).map(row => row.text)

  assert.ok(rows.some(row => row.includes('line one')), JSON.stringify(rows))
  assert.ok(rows.some(row => row.includes('line two')), JSON.stringify(rows))
})
