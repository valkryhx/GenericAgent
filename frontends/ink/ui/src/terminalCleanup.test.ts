import test from 'node:test'
import assert from 'node:assert/strict'
import {
  clearInlineLiveViewportSequence,
  enterMainScreenTerminalSequence,
  enterMainScreenTerminalSequenceForMode,
  exitTerminalCleanupSequence,
  exitTerminalCleanupSequenceForMode,
  reassertMouseTracking,
  resetViewportSequence,
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

test('fullscreen and mouse capture are independent switches', () => {
  // Alternate screen = layout; the 1000-series modes = pointer capture.
  const fullscreenFull = enterMainScreenTerminalSequenceForMode('full', true)
  assert.match(fullscreenFull, /\u001B\[\?1049h/)
  assert.match(fullscreenFull, /\u001B\[\?1000h/)
  assert.match(fullscreenFull, /\u001B\[\?1002h/)
  assert.match(fullscreenFull, /\u001B\[\?1006h/)
  // 1003h (bare any-motion) is never requested: it would report every cell the
  // pointer crosses. 1002h already covers motion while a button is held.
  assert.doesNotMatch(fullscreenFull, /\u001B\[\?1003h/)
  // Capturing the mouse means GA owns the wheel, so the terminal's
  // "alternate scroll" translation (which turns notches into arrow keys) is
  // turned off explicitly.
  assert.match(fullscreenFull, /\u001B\[\?1007l/)

  // Capture off: nothing in the 1000-series is emitted, so the terminal keeps
  // click-drag selection — and 1007 is left alone so its own scrolling works.
  const shipped = enterMainScreenTerminalSequenceForMode('off', true)
  assert.match(shipped, /\u001B\[\?1049h/)
  assert.doesNotMatch(shipped, /\u001B\[\?100[0-9]h/)
  assert.doesNotMatch(shipped, /\u001B\[\?1007l/)

  // Inline layout with capture off: no alternate screen either.
  const inline = enterMainScreenTerminalSequenceForMode('off', false)
  assert.doesNotMatch(inline, /\u001B\[\?1049h/)
  assert.doesNotMatch(inline, /\u001B\[\?100[0-9]h/)

  // Exit sequence must leave the alternate screen only when we entered it, and
  // must restore 1007 when we turned it off.
  assert.match(exitTerminalCleanupSequenceForMode('off', true), /\u001B\[\?1049l/)
  assert.doesNotMatch(exitTerminalCleanupSequenceForMode('off', false), /\u001B\[\?1049l/)
  assert.match(exitTerminalCleanupSequenceForMode('full', true), /\u001B\[\?1007h/)
  assert.doesNotMatch(exitTerminalCleanupSequenceForMode('off', true), /\u001B\[\?1007h/)

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

test('resetViewportSequence clears screen + scrollback and homes the cursor', () => {
  const sequence = resetViewportSequence()
  assert.ok(sequence.includes('\u001B[2J'), '必须清屏')
  assert.ok(sequence.includes('\u001B[3J'), '现代终端还要清 scrollback（否则重绘后历史会出现两份）')
  assert.ok(sequence.endsWith('\u001B[H'), '光标必须归位，供 ink 从行 0 全量重绘')
  assert.doesNotMatch(sequence, /\u001B\[?25l/)
  assert.doesNotMatch(sequence, /[\p{Extended_Pictographic}]/u)
})
