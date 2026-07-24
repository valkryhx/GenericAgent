import test from 'node:test'
import assert from 'node:assert/strict'
import {
  nextGraphemeOffset,
  previousGraphemeOffset,
  terminalSegments,
  wrapTerminalText,
} from './terminalText.js'

test('terminalSegments keeps emoji and combining marks intact', () => {
  assert.deepEqual(
    terminalSegments('A你👨‍💻e\u0301').map(segment => ({ text: segment.text, width: segment.width })),
    [
      { text: 'A', width: 1 },
      { text: '你', width: 2 },
      { text: '👨‍💻', width: 2 },
      { text: 'e\u0301', width: 1 },
    ],
  )
})

test('grapheme offset navigation never enters an emoji or combining sequence', () => {
  const text = 'A👨‍💻e\u0301Z'
  const emojiEnd = 'A👨‍💻'.length
  const accentEnd = 'A👨‍💻e\u0301'.length

  assert.equal(nextGraphemeOffset(text, 1), emojiEnd)
  assert.equal(previousGraphemeOffset(text, emojiEnd), 1)
  assert.equal(nextGraphemeOffset(text, emojiEnd), accentEnd)
  assert.equal(previousGraphemeOffset(text, accentEnd), emojiEnd)
})

test('wrapTerminalText soft-wraps by display columns and preserves source offsets', () => {
  assert.deepEqual(wrapTerminalText('ab你好cd', 4), [
    { text: 'ab你', width: 4, startOffset: 0, endOffset: 3 },
    { text: '好cd', width: 4, startOffset: 3, endOffset: 6 },
  ])
})

test('wrapTerminalText preserves explicit and trailing blank lines', () => {
  assert.deepEqual(wrapTerminalText('one\n', 8), [
    { text: 'one', width: 3, startOffset: 0, endOffset: 3 },
    { text: '', width: 0, startOffset: 4, endOffset: 4 },
  ])
})

test('wrapTerminalText does not split a ZWJ emoji', () => {
  assert.deepEqual(wrapTerminalText('ab👨‍💻cd', 4).map(line => line.text), ['ab👨‍💻', 'cd'])
})
