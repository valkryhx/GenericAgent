import test from 'node:test'
import assert from 'node:assert/strict'
import { enterMainScreenTerminalSequence, exitTerminalCleanupSequence } from './terminalCleanup.js'

test('exitTerminalCleanupSequence restores terminal state without visible symbols', () => {
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[0m'))
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?25h'))
  assert.doesNotMatch(exitTerminalCleanupSequence, /[\p{Extended_Pictographic}]/u)
})

test('main screen terminal sequences preserve native terminal scrollback', () => {
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?25l/)
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?25h'))
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?1049h/)
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?1006h/)
  assert.doesNotMatch(exitTerminalCleanupSequence, /\u001B\[\?1049l/)
  assert.doesNotMatch(exitTerminalCleanupSequence, /\u001B\[\?1006l/)
})
