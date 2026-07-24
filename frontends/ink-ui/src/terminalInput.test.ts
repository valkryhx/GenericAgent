import test from 'node:test'
import assert from 'node:assert/strict'
import { parseTerminalInput } from './terminalInput.js'

test('parseTerminalInput treats DEL as backspace and CSI 3 as delete', () => {
  assert.deepEqual(parseTerminalInput('\x7f'), {
    rawInput: '',
    key: {
      ctrl: false,
      shift: false,
      meta: false,
      alt: false,
      upArrow: false,
      downArrow: false,
      leftArrow: false,
      rightArrow: false,
      pageDown: false,
      pageUp: false,
      return: false,
      escape: false,
      tab: false,
      backspace: true,
      delete: false,
      sequence: '\x7f',
    },
  })
  assert.deepEqual(parseTerminalInput('\x1b[3~'), {
    rawInput: '',
    key: {
      ctrl: false,
      shift: false,
      meta: false,
      alt: false,
      upArrow: false,
      downArrow: false,
      leftArrow: false,
      rightArrow: false,
      pageDown: false,
      pageUp: false,
      return: false,
      escape: false,
      tab: false,
      backspace: false,
      delete: true,
      sequence: '\x1b[3~',
    },
  })
})

test('parseTerminalInput preserves text, arrows and ctrl shortcuts', () => {
  assert.deepEqual(parseTerminalInput('a'), {
    rawInput: 'a',
    key: {
      ctrl: false,
      shift: false,
      meta: false,
      alt: false,
      upArrow: false,
      downArrow: false,
      leftArrow: false,
      rightArrow: false,
      pageDown: false,
      pageUp: false,
      return: false,
      escape: false,
      tab: false,
      backspace: false,
      delete: false,
      sequence: 'a',
    },
  })
  assert.deepEqual(parseTerminalInput('\x1b[D').key.leftArrow, true)
  assert.deepEqual(parseTerminalInput('\x0f'), {
    rawInput: 'o',
    key: {
      ctrl: true,
      shift: false,
      meta: false,
      alt: false,
      upArrow: false,
      downArrow: false,
      leftArrow: false,
      rightArrow: false,
      pageDown: false,
      pageUp: false,
      return: false,
      escape: false,
      tab: false,
      backspace: false,
      delete: false,
      sequence: '\x0f',
    },
  })
})

test('parseTerminalInput preserves bracketed paste control sequences', () => {
  const input = '\x1b[200~a\nb\x1b[201~'

  assert.deepEqual(parseTerminalInput(input), {
    rawInput: input,
    key: {
      ctrl: false,
      shift: false,
      meta: false,
      alt: false,
      upArrow: false,
      downArrow: false,
      leftArrow: false,
      rightArrow: false,
      pageDown: false,
      pageUp: false,
      return: false,
      escape: false,
      tab: false,
      backspace: false,
      delete: false,
      sequence: input,
    },
  })
})
