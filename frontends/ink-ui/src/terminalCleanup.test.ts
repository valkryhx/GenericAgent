import test from 'node:test'
import assert from 'node:assert/strict'
import { exitTerminalCleanupSequence } from './terminalCleanup.js'

test('exitTerminalCleanupSequence restores terminal state without visible symbols', () => {
  assert.equal(exitTerminalCleanupSequence, '\u001B[0m\u001B[?25h\r\u001B[2K')
  assert.doesNotMatch(exitTerminalCleanupSequence, /[\p{Extended_Pictographic}]/u)
})
