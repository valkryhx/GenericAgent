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
