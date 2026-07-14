import test from 'node:test'
import assert from 'node:assert/strict'
import { formatAssistantText } from './messageFormat.js'

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

  assert.equal(formatAssistantText(raw), '> code_run(script: print(1), timeout: 60, type: python)\n[Action] Running python')
})

test('formatAssistantText expands verbose tool args when transcript mode is enabled', () => {
  const raw = [
    '🛠️ Tool: `code_run`  📥 args:',
    '````text',
    '{"script":"print(1)","timeout":60}',
    '````',
  ].join('\n')

  assert.equal(formatAssistantText(raw, { expanded: true }), [
    '> code_run',
    '  args:',
    '  {',
    '    "script": "print(1)",',
    '    "timeout": 60',
    '  }',
  ].join('\n'))
})

test('formatAssistantText renders compact tool calls without emoji', () => {
  const raw = '🛠️ code_run(type=python, script=print(1))\n\n\nDone'

  assert.equal(formatAssistantText(raw), '> code_run(type=python, script=print(1))\n\n\nDone')
})

test('formatAssistantText truncates long tool summaries with a ctrl+o hint', () => {
  const raw = [
    '🛠 Tool: `code_run`  📥 args:',
    '````text',
    JSON.stringify({ script: 'x'.repeat(140), timeout: 60 }),
    '````',
  ].join('\n')

  assert.match(formatAssistantText(raw), /^> code_run\(script: x{80}.*\) \(ctrl\+o to expand\)$/)
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
