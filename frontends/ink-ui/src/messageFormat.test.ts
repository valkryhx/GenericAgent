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
