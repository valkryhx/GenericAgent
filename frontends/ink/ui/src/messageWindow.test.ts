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
