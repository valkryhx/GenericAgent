import test from 'node:test'
import assert from 'node:assert/strict'
import { createPasteStore } from './paste.js'
import { handleInput } from './inputController.js'

test('handleInput submits expanded text when idle', () => {
  const store = createPasteStore()

  const decision = handleInput('hello', '', { return: true }, 'idle', store)

  assert.deepEqual(decision, { value: '', command: { type: 'submit', text: 'hello' } })
})

test('handleInput submits when Enter includes a raw carriage return', () => {
  const store = createPasteStore()

  const decision = handleInput('hello', '\r', { return: true }, 'idle', store)

  assert.deepEqual(decision, { value: '', command: { type: 'submit', text: 'hello' } })
})

test('handleInput keeps text when Enter is pressed while running', () => {
  const store = createPasteStore()

  const decision = handleInput('next prompt', '', { return: true }, 'running', store)

  assert.deepEqual(decision, { value: 'next prompt' })
})

test('handleInput inserts newlines with Claude-style Alt+Enter shortcut', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('hello', '', { meta: true, return: true }, 'idle', store), {
    value: 'hello\n',
  })
})

test('handleInput inserts newline for Alt+Enter raw carriage return', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('hello', '\r', {}, 'idle', store), {
    value: 'hello\n',
  })
})

test('handleInput inserts newline for Claude-style backslash Enter', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('hello\\', '', { return: true }, 'idle', store), {
    value: 'hello\n',
  })
})

test('handleInput moves cursor and edits at cursor offset', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('helo', '', { leftArrow: true }, 'idle', store, new Set(), 4), {
    value: 'helo',
    cursorOffset: 3,
  })
  assert.deepEqual(handleInput('helo', 'l', {}, 'idle', store, new Set(), 2), {
    value: 'hello',
    cursorOffset: 3,
  })
  assert.deepEqual(handleInput('hello', '', { backspace: true }, 'idle', store, new Set(), 3), {
    value: 'helo',
    cursorOffset: 2,
  })
  assert.deepEqual(handleInput('hello', '', { delete: true }, 'idle', store, new Set(), 2), {
    value: 'helo',
    cursorOffset: 2,
  })
})

test('handleInput treats terminal backspace bytes as backward delete', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('hello', '', { delete: true, sequence: '\x7f' }, 'idle', store, new Set(), 5), {
    value: 'hell',
    cursorOffset: 4,
  })
  assert.deepEqual(handleInput('hello', '', { delete: true, sequence: '\x7f' }, 'idle', store, new Set(), 3), {
    value: 'helo',
    cursorOffset: 2,
  })
  assert.deepEqual(handleInput('hello', '', { delete: true, sequence: '\x1b[3~' }, 'idle', store, new Set(), 3), {
    value: 'helo',
    cursorOffset: 3,
  })
})

test('handleInput sends stop for slash stop and Escape', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/stop', '', { return: true }, 'running', store), {
    value: '',
    command: { type: 'stop' },
  })
  assert.deepEqual(handleInput('draft', '', { escape: true }, 'running', store), {
    value: 'draft',
    command: { type: 'stop' },
  })
})

test('handleInput starts a fresh backend session for slash new and reset', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/new', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'new_session' },
  })
  assert.deepEqual(handleInput('/reset', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'new_session' },
  })
})

test('handleInput opens Claude-style local selectors for resume and rewind', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/resume', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_resume' },
  })
  assert.deepEqual(handleInput('/continue', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_resume' },
  })
  assert.deepEqual(handleInput('/rewind', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_rewind' },
  })
  assert.deepEqual(handleInput('/checkpoint', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_rewind' },
  })
})

test('handleInput sends indexed resume commands without opening a selector', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/resume 2', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'resume_session_index', index: 2 },
  })
  assert.deepEqual(handleInput('/continue 3', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'resume_session_index', index: 3 },
  })
})

test('handleInput parses mcp slash commands', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/mcp', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_mcp' },
  })
  assert.deepEqual(handleInput('/mcp reconnect demo server', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'mcp_reconnect', server: 'demo server' },
  })
  assert.deepEqual(handleInput('/mcp enable demo', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'mcp_enable', server: 'demo' },
  })
  assert.deepEqual(handleInput('/mcp disable demo', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'mcp_disable', server: 'demo' },
  })
})

test('handleInput parses model slash commands', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/model', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_model' },
  })
  assert.deepEqual(handleInput('/model kimi', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'model_switch', selector: 'kimi' },
  })
  assert.deepEqual(handleInput('/llm 1', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'model_switch', selector: '1' },
  })
  assert.deepEqual(handleInput('/model ?', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'model_status' },
  })
})

test('handleInput opens the local theme picker for slash theme', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/theme', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_theme' },
  })
})

test('handleInput parses compact slash commands', () => {
  const store = createPasteStore()
  assert.deepEqual(handleInput('/compact', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'compact', instructions: '' },
  })
  assert.deepEqual(handleInput('/compact keep decisions', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'compact', instructions: 'keep decisions' },
  })
})

test('handleInput parses known skill slash commands without using args as the skill name', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/imagegen 生成 一张图', '', { return: true }, 'idle', store, new Set(['imagegen'])), {
    value: '',
    command: { type: 'skill_invoke', skill: 'imagegen', args: '生成 一张图' },
  })
})

test('handleInput parses workflow list and control slash commands', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/workflows', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_list' },
  })
  assert.deepEqual(handleInput('/workflow', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_list' },
  })
  assert.deepEqual(handleInput('/workflow list', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_list' },
  })
  assert.deepEqual(handleInput('/workflow detail wf_demo', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_detail', runId: 'wf_demo' },
  })
  assert.deepEqual(handleInput('/workflow approve wf_demo', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_approve', runId: 'wf_demo' },
  })
  assert.deepEqual(handleInput('/workflow resume wf_demo', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_resume', runId: 'wf_demo' },
  })
  assert.deepEqual(handleInput('/workflow deny wf_demo no thanks', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_deny', runId: 'wf_demo', reason: 'no thanks' },
  })
  assert.deepEqual(handleInput('/workflow stop wf_demo user stop', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_stop', runId: 'wf_demo', reason: 'user stop' },
  })
})

test('handleInput parses workflow plan slash commands', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/workflow plan 调研 workflow UI', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_plan', taskText: '调研 workflow UI', autoApprove: true },
  })
  assert.deepEqual(handleInput('/workflow plan --manual 调研 workflow UI', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_plan', taskText: '调研 workflow UI', autoApprove: false },
  })
  assert.deepEqual(handleInput('/workflow plan --timeout 120 调研 workflow UI', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_plan', taskText: '调研 workflow UI', autoApprove: true, timeoutSeconds: 120 },
  })
  assert.deepEqual(handleInput('/workflow plan --manual --timeout 120 调研 workflow UI', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_plan', taskText: '调研 workflow UI', autoApprove: false, timeoutSeconds: 120 },
  })
  assert.deepEqual(handleInput('/workflow plan --timeout 120 --manual 调研 workflow UI', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_plan', taskText: '调研 workflow UI', autoApprove: false, timeoutSeconds: 120 },
  })
  assert.deepEqual(handleInput('/workflow plan 调研 workflow UI', '', { return: true }, 'idle', store, new Set(['workflow'])), {
    value: '',
    command: { type: 'workflow_plan', taskText: '调研 workflow UI', autoApprove: true },
  })
  assert.deepEqual(handleInput('/workflow plan', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_plan', taskText: '', autoApprove: true },
  })
})

test('handleInput folds multiline pasted text and expands on submit', () => {
  const store = createPasteStore()
  const pasted = handleInput('', '\u001b[200~a\r\nb\nc\u001b[201~', {}, 'idle', store)

  assert.equal(pasted.value, '[Copied text #1 +2 lines]')
  assert.deepEqual(handleInput(`${pasted.value} 请检查`, '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'submit', text: 'a\nb\nc 请检查' },
  })
})

test('handleInput folds split bracketed paste into a single copied placeholder', () => {
  const store = createPasteStore()

  const first = handleInput('', '\u001b[200~a\n', {}, 'idle', store)
  const second = handleInput(first.value, 'b\n', {}, 'idle', store)
  const third = handleInput(second.value, 'c\n', {}, 'idle', store)
  const done = handleInput(third.value, 'd\u001b[201~', {}, 'idle', store)

  assert.equal(done.value, '[Copied text #1 +3 lines]')
  assert.deepEqual(handleInput(`${done.value} summarize`, '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'submit', text: 'a\nb\nc\nd summarize' },
  })
})

test('handleInput merges adjacent multiline paste chunks into one placeholder', () => {
  const store = createPasteStore()

  const first = handleInput('', 'a\nb\n', {}, 'idle', store)
  const second = handleInput(first.value, 'c\nd', {}, 'idle', store)

  assert.equal(second.value, '[Copied text #1 +3 lines]')
  assert.deepEqual(handleInput(`${second.value} summarize`, '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'submit', text: 'a\nb\nc\nd summarize' },
  })
})

test('handleInput flushes unfinished bracketed paste on submit', () => {
  const store = createPasteStore()

  const pasted = handleInput('', '\u001b[200~a\nb', {}, 'idle', store)
  const submitted = handleInput(pasted.value, '', { return: true }, 'idle', store)

  assert.deepEqual(submitted, {
    value: '',
    command: { type: 'submit', text: 'a\nb' },
  })
})

test('handleInput compacts existing adjacent paste placeholders on submit', () => {
  const store = createPasteStore()
  store.set(3, 'a\nb\n')
  store.set(4, 'c\nd')

  const submitted = handleInput(
    '[Copied text #3 +2 lines][Copied text #4 +1 lines]',
    '',
    { return: true },
    'idle',
    store,
  )

  assert.deepEqual(submitted, {
    value: '',
    command: { type: 'submit', text: 'a\nb\nc\nd' },
  })
})

test('handleInput keeps split paste with single-line middle chunk as one placeholder', () => {
  const store = createPasteStore()

  const first = handleInput('', 'server:\nurl:\n', {}, 'idle', store)
  const second = handleInput(first.value, 'https://mcp.tavily.com/mcp/?tavilyApiKey=tvly-dev-', {}, 'idle', store)
  const third = handleInput(second.value, 'secret\nmore\nconfig', {}, 'idle', store)

  assert.equal(third.value, '[Copied text #1 +4 lines]')
  assert.ok(!third.value.includes('tavilyApiKey'))
  assert.deepEqual(handleInput(third.value, '', { return: true }, 'idle', store), {
    value: '',
    command: {
      type: 'submit',
      text: 'server:\nurl:\nhttps://mcp.tavily.com/mcp/?tavilyApiKey=tvly-dev-secret\nmore\nconfig',
    },
  })
})

test('handleInput turns Ctrl+C into shutdown and exit', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('draft', 'c', { ctrl: true }, 'idle', store), {
    value: 'draft',
    command: { type: 'shutdown' },
    exit: true,
  })
})
