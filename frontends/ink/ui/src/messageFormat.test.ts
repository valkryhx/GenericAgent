import test from 'node:test'
import assert from 'node:assert/strict'
import { formatAssistantText } from './messageFormat.js'
import { GLYPHS } from './glyphs.js'

test('formatAssistantText renders summary tags as plain text', () => {
  assert.equal(formatAssistantText('<summary>将写并运行质数脚本</summary>'), 'Summary: 将写并运行质数脚本')
})

test('formatAssistantText hides verbose tool args', () => {
  const raw = [
    '🛠️ Tool: `code_run`  📥 args:',
    '````text',
    '{"script":"print(1)","timeout":60,"type":"python"}',
    '````',
    '[Action] Running python',
  ].join('\n')

  assert.equal(formatAssistantText(raw), `${GLYPHS.bullet} code_run(script: print(1), timeout: 60, type: python)\n[Action] Running python`)
})

test('formatAssistantText expands verbose tool args when transcript mode is enabled', () => {
  const raw = [
    '🛠️ Tool: `code_run`  📥 args:',
    '````text',
    '{"script":"print(1)","timeout":60}',
    '````',
  ].join('\n')

  assert.equal(formatAssistantText(raw, { expanded: true }), [
    `${GLYPHS.bullet} code_run`,
    '  args:',
    '  {',
    '    "script": "print(1)",',
    '    "timeout": 60',
    '  }',
  ].join('\n'))
})

test('formatAssistantText renders compact tool calls without emoji', () => {
  const raw = '🛠️ code_run(type=python, script=print(1))\n\n\nDone'

  assert.equal(formatAssistantText(raw), `${GLYPHS.bullet} code_run(type=python, script=print(1))\n\n\nDone`)
})

test('formatAssistantText truncates long tool summaries with a ctrl+o hint', () => {
  const raw = [
    '🛠 Tool: `code_run`  📥 args:',
    '````text',
    JSON.stringify({ script: 'x'.repeat(140), timeout: 60 }),
    '````',
  ].join('\n')

  assert.match(formatAssistantText(raw), new RegExp(`^${GLYPHS.bullet} code_run\\(script: x{80}.*\\) \\(ctrl\\+o to expand\\)$`))
})

test('formatAssistantText removes final response marker block', () => {
  const raw = [
    '这是最终回答。',
    '',
    '`````',
    '[Info] Final response to user.',
    '`````',
  ].join('\n')

  assert.equal(formatAssistantText(raw), '这是最终回答。')
})

test('formatAssistantText collapses tool fence noise without dropping final bold answer', () => {
  const raw = [
    '**LLM Running (Turn 1) ...**',
    '',
    '<summary>需要当前时间，读取系统钟</summary>',
    '',
    '🛠️ Tool: `code_run`  📥 args:',
    '````text',
    '{"script":"print(1)","type":"python"}',
    '````',
    '`````',
    '[Action] Running python',
    '[Status] Exit Code: 0',
    '[Stdout]',
    '2026-07-14 15:28:09 Tuesday',
    '`````',
    '',
    '**LLM Running (Turn 2) ...**',
    '',
    '<summary>系统时间为15:28</summary>',
    '',
    '现在是 **2026年7月14日 15:28**。',
  ].join('\n')

  const formatted = formatAssistantText(raw)
  assert.match(formatted, /现在是 \*\*2026年7月14日 15:28\*\*。/)
  assert.doesNotMatch(formatted, /^:28\*\*/m)
  assert.equal(formatted.split('LLM Running (Turn 1)').length - 1, 1)
})

test('formatAssistantText strips GA tool status fences so later turns stay markdown bold', () => {
  const raw = [
    '`````',
    '[Action] Calling MCP tool: mcp__tavily__tavily_search',
    '[Status] MCP success',
    '`````',
    '',
    '**LLM Running (Turn 2) ...**',
    '',
    'Summary: second pass',
  ].join('\n')
  const formatted = formatAssistantText(raw)
  assert.match(formatted, /\[Action\] Calling MCP tool/)
  assert.doesNotMatch(formatted, /```/)
  // After format, markdown renderer should still bold Turn 2 (no open fence).
  assert.match(formatted, /\*\*LLM Running \(Turn 2\) \.\.\.\*\*/)
})

test('formatAssistantText folds long [Stdout] bodies until transcript mode', () => {
  const body = Array.from({ length: 12 }, (_, i) => `line ${i + 1}`).join('\n')
  const raw = ['`````', '[Status] Exit Code: 0', '[Stdout]', body, '`````'].join('\n')
  const collapsed = formatAssistantText(raw)
  assert.match(collapsed, /\[Stdout\]/)
  assert.match(collapsed, /line 8/)
  assert.doesNotMatch(collapsed, /line 9/)
  assert.match(collapsed, /\(\+4 more lines\) \(ctrl\+o to expand\)/)
  const expanded = formatAssistantText(raw, { expanded: true })
  assert.match(expanded, /line 12/)
  assert.doesNotMatch(expanded, /more lines/)
})

test('formatAssistantText folds the hammer-emoji tool header too', () => {
  // agent_loop.py emits a hammer emoji, continue_cmd.py a tools emoji; both must fold.
  const raw = '🔨 Tool: `load_skill`  📥 args:\n````text\n{"name":"workflow-authoring"}\n````'
  assert.equal(formatAssistantText(raw), `${GLYPHS.bullet} load_skill(name: workflow-authoring)`)
})

test('formatAssistantText collapses a tool header before its args fence arrives', () => {
  const raw = '🔨 Tool: `load_skill`  📥 args:'
  assert.equal(formatAssistantText(raw), `${GLYPHS.bullet} load_skill`)
})

test('formatAssistantText streaming folds an open tool args fence to a chip', () => {
  // First-turn code_run regression: the tool header was committed to Static and
  // only the open ```text JSON remained in the live segment. It must never be
  // painted line-by-line into the 12-row viewport.
  const partial = [
    '我先执行一个脚本。',
    '⏺ Tool: `code_run` args:',
    '````text',
    '{',
    '  "script": "import os',
    ...Array.from({ length: 60 }, (_, index) => `  line_${index} = ${index}`),
  ].join('\n')

  assert.equal(formatAssistantText(partial, { streaming: true }), [
    '我先执行一个脚本。',
    `${GLYPHS.bullet} code_run`,
  ].join('\n'))
})

test('formatAssistantText summarizes multi-line code_run args instead of dumping raw JSON', () => {
  // Historical transcripts stored invalid JSON because the old
  // loop.get_pretty_json() replaced escaped newlines with real ones. It
  // must still become one chip when an old session is reopened.
  const args = [
    '{',
    '  "script": "import os',
    'print(os.getcwd())',
    'print(1)",',
    '  "timeout": 60,',
    '  "type": "python"',
    '}',
  ].join('\n')
  const raw = [`⏺ Tool: \`code_run\` args:`, '````text', args, '````'].join('\n')
  const formatted = formatAssistantText(raw)
  assert.match(formatted, /^● code_run\(script: import os print\(os\.getcwd\(\)\) print\(1\), timeout: 60, type: python\)/)
  assert.doesNotMatch(formatted, /^\s*"script"/m)
})

test('formatAssistantText folds an orphan args fence left by a static/live split', () => {
  // Historical first-turn bug: the tool chip was in the earlier Static segment
  // and only this bare ```text args block remained. It must not render as cyan
  // JSON prose.
  const raw = ['````text', '{"script":"print(1)","timeout":60}', '````', '[Status] Exit Code: 0'].join('\n')
  const formatted = formatAssistantText(raw)
  assert.doesNotMatch(formatted, /"script"/)
  assert.match(formatted, /\[Status\] Exit Code: 0/)
})

test('formatAssistantText streaming keeps complete tool blocks collapsed', () => {
  const complete = [
    '⏺ Tool: `code_run` args:',
    '````text',
    '{"script":"print(1)","timeout":60}',
    '````',
  ].join('\n')

  assert.equal(
    formatAssistantText(complete, { streaming: true }),
    `${GLYPHS.bullet} code_run(script: print(1), timeout: 60)`,
  )
})

test('formatAssistantText matches five-backtick tool arg fences', () => {
  const raw = [
    '⏺ Tool: `code_run` args:',
    '`````text',
    '{"script":"print(1)","timeout":60}',
    '`````',
  ].join('\n')

  assert.equal(formatAssistantText(raw), `${GLYPHS.bullet} code_run(script: print(1), timeout: 60)`)
})

test('formatAssistantText rejoins a tool chip with a later args fence', () => {
  const raw = [
    `${GLYPHS.bullet} code_run`,
    '````text',
    '{"script":"print(1)","timeout":60}',
    '````',
  ].join('\n')

  assert.equal(formatAssistantText(raw), `${GLYPHS.bullet} code_run(script: print(1), timeout: 60)`)
})

test('formatAssistantText repairs a backslash followed by a real newline in legacy args', () => {
  // Old get_pretty_json() replaced every literal backslash-n with a real
  // newline, so this historical shape has a backslash immediately before a
  // physical newline. The chip must not leak that broken pair into the summary.
  const bs = String.fromCharCode(92)
  const newline = String.fromCharCode(10)
  const args = [
    '{',
    '  "script": "print(1)' + bs + newline + 'print(2)",',
    '  "timeout": 60',
    '}',
  ].join(newline)
  const raw = ['⏺ Tool: `code_run` args:', '````text', args, '````'].join(newline)
  const formatted = formatAssistantText(raw)

  assert.match(formatted, /^● code_run\(script: print\(1\)\\nprint\(2\), timeout: 60\)/)
  assert.equal(formatted.includes(bs + newline), false)
})

test('formatAssistantText leaves an ordinary 3-backtick code block with a [Status] line intact', () => {
  const raw = [
    'Output example:',
    '```',
    '[Status] not a GA tool marker',
    'print(1)',
    '```',
  ].join('\n')
  const formatted = formatAssistantText(raw)

  assert.match(formatted, /```/)
  assert.match(formatted, /\[Status\] not a GA tool marker/)
})

test('formatAssistantText keeps ordinary markdown code fences intact', () => {
  const raw = [
    'Here:',
    '~~~python',
    'print(1)',
    '~~~',
    'Done',
  ].join('\n')

  const formatted = formatAssistantText(raw)
  assert.match(formatted, /~~~python/)
  assert.match(formatted, /print\(1\)/)
})
