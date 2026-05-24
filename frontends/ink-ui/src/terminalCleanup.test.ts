import test from 'node:test'
import assert from 'node:assert/strict'
import { enterMainScreenTerminalSequence, exitTerminalCleanupSequence, reassertMouseTracking } from './terminalCleanup.js'

test('exitTerminalCleanupSequence restores terminal state without visible symbols', () => {
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[0m'))
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?25h'))
  assert.doesNotMatch(exitTerminalCleanupSequence, /[\p{Extended_Pictographic}]/u)
})

test('fullscreen terminal sequences use alternate screen with mouse wheel tracking', () => {
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?25l/)
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?25h'))
  assert.ok(enterMainScreenTerminalSequence.includes('\u001B[?1049h'))
  assert.ok(enterMainScreenTerminalSequence.includes('\u001B[2J\u001B[H'))
  assert.ok(enterMainScreenTerminalSequence.includes('\u001B[?1006h'))
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?1049l'))
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?1006l'))
})

test('reassertMouseTracking restores mouse reporting without clearing the alternate screen', () => {
  const chunks: string[] = []
  reassertMouseTracking({ write: chunk => {
    chunks.push(String(chunk))
    return true
  } })

  assert.equal(chunks.join(''), '\u001B[?1000h\u001B[?1002h\u001B[?1003h\u001B[?1006h')
  assert.doesNotMatch(chunks.join(''), /\u001B\[\?1049h/)
  assert.doesNotMatch(chunks.join(''), /\u001B\[2J/)
})
