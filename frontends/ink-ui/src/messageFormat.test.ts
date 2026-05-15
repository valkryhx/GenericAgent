import test from 'node:test'
import assert from 'node:assert/strict'
import { formatAssistantText } from './messageFormat.js'

test('formatAssistantText renders summary tags as plain text', () => {
  assert.equal(formatAssistantText('<summary>将写并运行质数脚本</summary>'), 'Summary: 将写并运行质数脚本')
})

test('formatAssistantText hides verbose tool args', () => {
  const raw = [
    '🛠 Tool: `code_run`  📥 args:',
    '````text',
    '{"script":"very long"}',
    '````',
    '[Action] Running python',
  ].join('\n')

  assert.equal(formatAssistantText(raw), 'Tool: code_run (args hidden)\n[Action] Running python')
})
