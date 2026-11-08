import test from 'node:test'
import assert from 'node:assert/strict'
import { createPasteStore } from './paste.js'
import { createImageAttachmentStore } from './imageAttachments.js'
import { handleInput } from './inputController.js'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { randomBytes } from 'node:crypto'

const imgRoot = join(tmpdir(), `ga-ic-img-${randomBytes(4).toString('hex')}`)
mkdirSync(imgRoot, { recursive: true })
const IMG_BYTES = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGM8YWTEgA0wYRUdtBIA76YBPGvKGPUAAAAASUVORK5CYII=',
  'base64',
)
function mkImg(name: string): string {
  const p = join(imgRoot, name)
  writeFileSync(p, IMG_BYTES)
  return p
}

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

test('handleInput queues text when Enter is pressed while running', () => {
  // Enter used to keep the text and do nothing at all. That is indistinguishable from
  // a hung app: no frame, no message, no state change. The draft is now queued and
  // folded into the running turn at the next tool boundary, so the user's intent
  // survives instead of sitting in a box they cannot tell is alive.
  const store = createPasteStore()

  const decision = handleInput('next prompt', '', { return: true }, 'running', store)

  // `cursorOffset` is only echoed when the caller tracks one (the App passes it);
  // this call does not, so the decision carries just the value and the command.
  assert.deepEqual(decision, {
    value: '',
    command: { type: 'queue_prompt', text: 'next prompt' },
  })
})

test('handleInput still swallows Enter while connecting', () => {
  // No turn exists yet and the bridge handshake has not completed, so queueing would
  // promise a delivery that cannot happen either.
  const store = createPasteStore()

  const decision = handleInput('next prompt', '', { return: true }, 'connecting', store)

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

test('handleInput moves and deletes whole grapheme clusters', () => {
  const store = createPasteStore()
  const value = 'A👨‍💻e\u0301Z'
  const afterEmoji = 'A👨‍💻'.length
  const afterAccent = 'A👨‍💻e\u0301'.length

  assert.equal(handleInput(value, '', { leftArrow: true }, 'idle', store, new Set(), afterAccent).cursorOffset, afterEmoji)
  assert.equal(handleInput(value, '', { rightArrow: true }, 'idle', store, new Set(), 1).cursorOffset, afterEmoji)
  assert.deepEqual(handleInput(value, '', { backspace: true }, 'idle', store, new Set(), afterEmoji), {
    value: 'Ae\u0301Z',
    cursorOffset: 1,
  })
  assert.deepEqual(handleInput(value, '', { delete: true }, 'idle', store, new Set(), afterEmoji), {
    value: 'A👨‍💻Z',
    cursorOffset: afterEmoji,
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

test('handleInput supports explicit legacy views for resume and workflow', () => {
  const store = createPasteStore()
  assert.deepEqual(handleInput('/resume --all', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_resume', includeLegacy: true },
  })
  assert.deepEqual(handleInput('/continue 3 --all', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'resume_session_index', index: 3, includeLegacy: true },
  })
  assert.deepEqual(handleInput('/workflows --all', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_list', includeLegacy: true },
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

test('handleInput parses permission slash commands', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/permissions', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_permissions' },
  })
  assert.deepEqual(handleInput('/perms', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'open_permissions' },
  })
  assert.deepEqual(handleInput('/permissions read only', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'set_permission_mode', mode: 'read_only' },
  })
  assert.deepEqual(handleInput('/permissions ask', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'set_permission_mode', mode: 'ask' },
  })
  assert.deepEqual(handleInput('/permissions full-access', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'set_permission_mode', mode: 'full_access' },
  })
  assert.deepEqual(handleInput('/permissions ?', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'permission_status' },
  })
})

test('handleInput opens the skills cost panel for slash skills', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/skills', '', { return: true }, 'idle', store), {
    value: '',
    action: { type: 'skills' },
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

test('handleInput parses directory-scoped skill slash commands', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/apps/web:deploy --env prod', '', { return: true }, 'idle', store, new Set(['apps/web:deploy'])), {
    value: '',
    command: { type: 'skill_invoke', skill: 'apps/web:deploy', args: '--env prod' },
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
  assert.deepEqual(handleInput('/workflow resume wf_demo', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_resume', runId: 'wf_demo' },
  })
  assert.deepEqual(handleInput('/workflow stop wf_demo user stop', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_stop', runId: 'wf_demo', reason: 'user stop' },
  })
  // CC "save as named workflow" + "run a saved workflow by name" (m1906)
  assert.deepEqual(handleInput('/workflow save wf_demo demo-flow', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_save', runId: 'wf_demo', name: 'demo-flow' },
  })
  assert.deepEqual(handleInput('/workflow save wf_demo demo-flow --overwrite', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_save', runId: 'wf_demo', name: 'demo-flow', overwrite: true },
  })
  assert.deepEqual(handleInput('/workflow save wf_demo', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_save', runId: 'wf_demo' },
  })
  assert.deepEqual(handleInput('/workflow run demo-flow', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_run_named', name: 'demo-flow' },
  })
  assert.deepEqual(handleInput('/workflows saved', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'workflow_named_list' },
  })
  // approve/deny are no longer user-facing slash commands (swallowed, not chat)
  assert.deepEqual(handleInput('/workflow approve wf_demo', '', { return: true }, 'idle', store), {
    value: '',
  })
  assert.deepEqual(handleInput('/workflow deny wf_demo no thanks', '', { return: true }, 'idle', store), {
    value: '',
  })
})

test('handleInput treats /workflow on|off as session opt-in and /workflow <task> as chat', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/workflow on', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'submit', text: '/workflow on' },
  })
  assert.deepEqual(handleInput('/workflow off', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'submit', text: '/workflow off' },
  })
  // The old JSON-planner start path is gone: a bare /workflow <task> is not a
  // command any more, so it falls through to an ordinary chat submit. The host
  // still injects the workflow reminder because the task text is opt-in shaped.
  assert.deepEqual(handleInput('/workflow 调研 workflow UI', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'submit', text: '/workflow 调研 workflow UI' },
  })
  assert.deepEqual(handleInput('/workflow plan 调研 workflow UI', '', { return: true }, 'idle', store), {
    value: '',
    command: { type: 'submit', text: '/workflow plan 调研 workflow UI' },
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

test('handleInput attaches image path paste as [Image #N] and submits images', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const path = mkImg('demo.png')
  const pasted = handleInput('', path, {}, 'idle', store, new Set(), undefined, images)
  assert.equal(pasted.value, '[Image #1]')
  const submitted = handleInput(pasted.value, '', { return: true }, 'idle', store, new Set(), undefined, images)
  assert.equal(submitted.value, '')
  assert.equal(submitted.command?.type, 'submit')
  if (submitted.command?.type === 'submit') {
    assert.equal(submitted.command.text, '[Image #1]')
    assert.deepEqual(submitted.command.images, [
      { path, placeholder: '[Image #1]', source: 'path' },
    ])
  }
})

test('handleInput joins split wechat image path paste without mid-filename replace', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const full = mkImg('微信图片_20260720144105_2048_89.jpg')
  const cut = full.indexOf('_2048_89.jpg')
  const head = full.slice(0, cut + 2)
  const tail = full.slice(cut + 2) + ' 这图是什么'

  const first = handleInput('', head, {}, 'idle', store, new Set(), 0, images)
  assert.equal(first.value, head)
  assert.equal(images.byId.size, 0)

  const second = handleInput(first.value, tail, {}, 'idle', store, new Set(), first.value.length, images)
  assert.equal(second.value, '[Image #1] 这图是什么')
  assert.equal(images.byId.get(1)?.path, full)
  assert.ok(!second.value.includes('048_89'))

  const submitted = handleInput(second.value, '', { return: true }, 'idle', store, new Set(), second.value.length, images)
  assert.equal(submitted.command?.type, 'submit')
  if (submitted.command?.type === 'submit') {
    assert.equal(submitted.command.text, '[Image #1] 这图是什么')
    assert.deepEqual(submitted.command.images, [
      { path: full, placeholder: '[Image #1]', source: 'path' },
    ])
  }
})

test('handleInput attaches spaced screenshot path in one paste', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const path = mkImg('屏幕截图 2026-07-15 124017.png')
  const pasted = handleInput('', `${path} 这图是什么`, {}, 'idle', store, new Set(), 0, images)
  assert.equal(pasted.value, '[Image #1] 这图是什么')
  assert.equal(images.byId.get(1)?.path, path)

  const submitted = handleInput(pasted.value, '', { return: true }, 'idle', store, new Set(), pasted.value.length, images)
  assert.equal(submitted.command?.type, 'submit')
  if (submitted.command?.type === 'submit') {
    assert.equal(submitted.command.text, '[Image #1] 这图是什么')
    assert.deepEqual(submitted.command.images, [
      { path, placeholder: '[Image #1]', source: 'path' },
    ])
  }
})

test('handleInput PowerShell & quoted spaced path becomes clean [Image #N]', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const path = mkImg('屏幕截图 2026-07-15 124017.png')
  const raw = `& '${path}'`
  const pasted = handleInput('', raw, {}, 'idle', store, new Set(), 0, images)
  assert.equal(pasted.value, '[Image #1]')
  assert.ok(!pasted.value.includes('&'))
  assert.ok(!pasted.value.includes("'"))
  assert.equal(images.byId.get(1)?.path, path)
})

test('handleInput joins spaced screenshot path split after space', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const full = mkImg('屏幕截图 2026-07-15 124017.png')
  const cut = full.lastIndexOf(' ')
  const head = full.slice(0, cut + 1)
  const tail = full.slice(cut + 1) + ' 看看'
  const first = handleInput('', head, {}, 'idle', store, new Set(), 0, images)
  const second = handleInput(first.value, tail, {}, 'idle', store, new Set(), first.value.length, images)
  assert.equal(second.value, '[Image #1] 看看')
  assert.equal(images.byId.get(1)?.path, full)
})

test('handleInput does not treat relative image-like suffix alone as attachment', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const decision = handleInput('', '048_89.jpg', {}, 'idle', store, new Set(), 0, images)
  assert.equal(decision.value, '048_89.jpg')
  assert.equal(images.byId.size, 0)
})

test('isImagePasteShortcut accepts ctrl+v, alt+v, ctrl+alt+v', async () => {
  const { isImagePasteShortcut } = await import('./inputController.js')
  assert.equal(isImagePasteShortcut({ ctrl: true }, 'v'), true)
  assert.equal(isImagePasteShortcut({ ctrl: true, sequence: '\x16' }, 'v'), true)
  assert.equal(isImagePasteShortcut({ alt: true }, 'v'), true)
  assert.equal(isImagePasteShortcut({ meta: true }, 'v'), true)
  assert.equal(isImagePasteShortcut({ ctrl: true, alt: true }, 'v'), true)
  assert.equal(isImagePasteShortcut({}, 'v'), false)
  assert.equal(isImagePasteShortcut({ ctrl: true }, 'c'), false)
})

test('parseTerminalInput maps alt+v and ctrl+v', async () => {
  const { parseTerminalInput } = await import('./terminalInput.js')
  const altV = parseTerminalInput('\x1bv')
  assert.equal(altV.rawInput, 'v')
  assert.equal(altV.key.meta, true)
  assert.equal(altV.key.alt, true)

  const ctrlV = parseTerminalInput('\x16')
  assert.equal(ctrlV.rawInput, 'v')
  assert.equal(ctrlV.key.ctrl, true)
})

test('handleInput image paste shortcut marks pendingImagePaste without inserting v', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const decision = handleInput('hi', 'v', { alt: true }, 'idle', store, new Set(), 2, images)
  assert.equal(decision.value, 'hi')
  assert.equal(decision.pendingImagePaste, true)
  assert.notEqual(decision.value, 'hiv')
})

test('handleInput alt+v does not insert letter v (clipboard image or no-op)', async () => {
  const mod = await import('./inputController.js')
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const decision = mod.handleInput('hi', 'v', { alt: true }, 'idle', store, new Set(), 2, images)
  // 异步路径：只标 pending，绝不插入字母 v
  assert.notEqual(decision.value, 'hiv')
  assert.ok(decision.pendingImagePaste === true || decision.value === 'hi' || decision.value.includes('[Image #'))
})

test('handleInput maps /hooks to the hook inventory command', () => {
  const store = createPasteStore()
  assert.deepEqual(handleInput('/hooks', '', { return: true }, 'idle', store), { value: '', command: { type: 'hooks_status' } })
})

test('handleInput maps /team and /agents to the team roster command', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/team', '', { return: true }, 'idle', store), { value: '', command: { type: 'team_list' } })
  assert.deepEqual(handleInput('/agents', '', { return: true }, 'idle', store), { value: '', command: { type: 'team_list' } })
  assert.deepEqual(handleInput('/team list', '', { return: true }, 'idle', store), { value: '', command: { type: 'team_list' } })
})

test('handleInput forwards /team on|off to the bridge as a submit', () => {
  const store = createPasteStore()

  assert.deepEqual(handleInput('/team on', '', { return: true }, 'idle', store), { value: '', command: { type: 'submit', text: '/team on' } })
  assert.deepEqual(handleInput('/team off', '', { return: true }, 'idle', store), { value: '', command: { type: 'submit', text: '/team off' } })
})

test('imagePastePolicy distinguishes ctrl+v (image-then-text) from alt/ctrl+alt (image-only)', async () => {
  const { imagePastePolicy } = await import('./inputController.js')
  assert.equal(imagePastePolicy({ ctrl: true }, 'v'), 'image-then-text')
  assert.equal(imagePastePolicy({ ctrl: true, sequence: '\x16' }, 'v'), 'image-then-text')
  assert.equal(imagePastePolicy({ alt: true }, 'v'), 'image-only')
  assert.equal(imagePastePolicy({ meta: true }, 'v'), 'image-only')
  assert.equal(imagePastePolicy({ ctrl: true, alt: true }, 'v'), 'image-only')
  assert.equal(imagePastePolicy({}, 'v'), null)
  assert.equal(imagePastePolicy({ ctrl: true }, 'c'), null)
})

test('handleInput pending image paste carries policy', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const ctrl = handleInput('hi', 'v', { ctrl: true }, 'idle', store, new Set(), 2, images)
  assert.equal(ctrl.pendingImagePaste, true)
  assert.equal(ctrl.imagePastePolicy, 'image-then-text')
  const alt = handleInput('hi', 'v', { alt: true }, 'idle', store, new Set(), 2, images)
  assert.equal(alt.pendingImagePaste, true)
  assert.equal(alt.imagePastePolicy, 'image-only')
})

test('handleInput does not attach a non-existent image path (O7), keeps text + notice', () => {
  const store = createPasteStore()
  const images = createImageAttachmentStore()
  const missing = String.raw`D:\nope_does_not_exist_zz\gone.png`
  const decision = handleInput('', missing, {}, 'idle', store, new Set(), 0, images)
  assert.equal(images.byId.size, 0)
  assert.ok(decision.value.includes('gone.png'))
  assert.ok(decision.notice && decision.notice.includes('找不到文件'))
})

test('isUsableImagePath rejects missing file and accepts real file', async () => {
  const { isUsableImagePath } = await import('./inputController.js')
  assert.equal(isUsableImagePath(String.raw`D:\nope_zz\gone.png`), false)
  const real = mkImg('usable-check.png')
  assert.equal(isUsableImagePath(real), true)
})
