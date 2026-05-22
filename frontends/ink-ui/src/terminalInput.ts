import type { InputKey } from './inputController.js'

const nonTextKeyNames = new Set([
  'up',
  'down',
  'left',
  'right',
  'pagedown',
  'pageup',
  'return',
  'escape',
  'tab',
  'delete',
  'backspace',
])

type ParsedTerminalKey = {
  name: string
  ctrl: boolean
  meta: boolean
  shift: boolean
  sequence: string
}

export function parseTerminalInput(input: string): { rawInput: string; key: InputKey } {
  const parsed = parseTerminalKey(input)
  const key: InputKey = {
    ctrl: parsed.ctrl,
    shift: parsed.shift,
    meta: parsed.meta || parsed.name === 'escape',
    upArrow: parsed.name === 'up',
    downArrow: parsed.name === 'down',
    leftArrow: parsed.name === 'left',
    rightArrow: parsed.name === 'right',
    pageDown: parsed.name === 'pagedown',
    pageUp: parsed.name === 'pageup',
    return: parsed.name === 'return',
    escape: parsed.name === 'escape',
    tab: parsed.name === 'tab',
    backspace: parsed.name === 'backspace',
    delete: parsed.name === 'delete',
    sequence: parsed.sequence,
  }
  let rawInput = parsed.ctrl ? parsed.name : parsed.sequence
  if (nonTextKeyNames.has(parsed.name)) rawInput = ''
  if (parsed.meta && rawInput.startsWith('\u001B')) rawInput = rawInput.slice(1)
  if (rawInput.length === 1 && /[A-Z]/.test(rawInput[0])) key.shift = true
  return { rawInput, key }
}

function parseTerminalKey(input: string): ParsedTerminalKey {
  const key: ParsedTerminalKey = { name: '', ctrl: false, meta: false, shift: false, sequence: input }

  if (input === '\r') key.name = 'return'
  else if (input === '\n') key.name = 'enter'
  else if (input === '\t') key.name = 'tab'
  else if (input === '\b' || input === '\x7f') key.name = 'backspace'
  else if (input === '\x1b\b' || input === '\x1b\x7f') {
    key.name = 'backspace'
    key.meta = true
  } else if (input === '\x1b') {
    key.name = 'escape'
  } else if (input === '\x1b[A') key.name = 'up'
  else if (input === '\x1b[B') key.name = 'down'
  else if (input === '\x1b[C') key.name = 'right'
  else if (input === '\x1b[D') key.name = 'left'
  else if (input === '\x1b[5~') key.name = 'pageup'
  else if (input === '\x1b[6~') key.name = 'pagedown'
  else if (input === '\x1b[3~') key.name = 'delete'
  else if (input.length === 1 && input <= '\x1a') {
    key.name = String.fromCharCode(input.charCodeAt(0) + 'a'.charCodeAt(0) - 1)
    key.ctrl = true
  } else if (input.length === 1 && input >= 'A' && input <= 'Z') {
    key.name = input.toLowerCase()
    key.shift = true
  } else if (input.startsWith('\x1b') && input.length === 2) {
    key.name = input[1]
    key.meta = true
    key.shift = /^[A-Z]$/.test(input[1])
  }

  return key
}
