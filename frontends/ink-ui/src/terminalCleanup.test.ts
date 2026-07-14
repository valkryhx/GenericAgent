import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clearInlineLiveViewportSequence,
  enterMainScreenTerminalSequence,
  enterMainScreenTerminalSequenceForMode,
  exitTerminalCleanupSequence,
  reassertMouseTracking,
} from './terminalCleanup.js'

test('exitTerminalCleanupSequence restores terminal state without visible symbols', () => {
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[0m'))
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?25h'))
  assert.doesNotMatch(exitTerminalCleanupSequence, /[\p{Extended_Pictographic}]/u)
})

test('default terminal sequence uses inline screen without mouse capture', () => {
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?25l/)
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?25h'))
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?1049h/)
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[2J\u001B\[H/)
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?1000h/)
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?1002h/)
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?1003h/)
  assert.doesNotMatch(enterMainScreenTerminalSequence, /\u001B\[\?1006h/)
  assert.ok(exitTerminalCleanupSequence.includes('\u001B[?1006l'))
})

test('reassertMouseTracking is a no-op by default', () => {
  const chunks: string[] = []
  reassertMouseTracking({ write: chunk => {
    chunks.push(String(chunk))
    return true
  } })

  assert.equal(chunks.join(''), '')
  assert.doesNotMatch(chunks.join(''), /\u001B\[\?1049h/)
  assert.doesNotMatch(chunks.join(''), /\u001B\[2J/)
})

test('full mouse mode keeps the legacy alternate screen and mouse capture path', () => {
  const sequence = enterMainScreenTerminalSequenceForMode('full')

  assert.match(sequence, /\u001B\[\?1049h/)
  assert.match(sequence, /\u001B\[\?1000h/)
  assert.match(sequence, /\u001B\[\?1002h/)
  assert.match(sequence, /\u001B\[\?1003h/)
  assert.match(sequence, /\u001B\[\?1006h/)

  const chunks: string[] = []
  reassertMouseTracking({ write: chunk => {
    chunks.push(String(chunk))
    return true
  } }, 'full')
  assert.match(chunks.join(''), /\u001B\[\?1006h/)
})

test('clearInlineLiveViewportSequence clears the current inline UI block and returns to its top', () => {
  const sequence = clearInlineLiveViewportSequence({ rows: 7, cursorRow: 5 })

  assert.equal(sequence.startsWith('\u001B[0m\u001B[5A\r'), true)
  assert.equal((sequence.match(/\u001B\[2K/g) ?? []).length, 7)
  assert.equal((sequence.match(/\u001B\[1B/g) ?? []).length, 6)
  assert.equal(sequence.endsWith('\u001B[6A\r'), true)
  assert.doesNotMatch(sequence, /\u001B\[\?1049h/)
  assert.doesNotMatch(sequence, /\u001B\[\?1000h/)
})

test('clearInlineLiveViewportSequence clears when the cursor is below the inline UI block', () => {
  const sequence = clearInlineLiveViewportSequence({ rows: 7, cursorRow: 7 })

  assert.equal(sequence.startsWith('\u001B[0m\u001B[7A\r'), true)
  assert.equal((sequence.match(/\u001B\[2K/g) ?? []).length, 7)
  assert.equal(sequence.endsWith('\u001B[6A\r'), true)
})
