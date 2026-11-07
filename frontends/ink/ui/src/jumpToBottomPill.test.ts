import test from 'node:test'
import assert from 'node:assert/strict'
import {
  countNewMessages,
  countStepHeaders,
  hitTestJumpToBottom,
  isStepHeaderLine,
  jumpToBottomLabel,
  jumpToBottomPlacement,
  jumpToBottomText,
} from './jumpToBottomPill.js'

test('isStepHeaderLine matches every spelling the runtime emits', () => {
  // `loop.py` emits `**LLM Running (Step N) ...**` (verbose) or `**Step N ...**`
  // (task dir mode); the bold markers may or may not survive into the stream.
  assert.equal(isStepHeaderLine('**LLM Running (Step 1) ...**'), true)
  assert.equal(isStepHeaderLine('LLM Running (Step 12) ...'), true)
  assert.equal(isStepHeaderLine('**Step 3 ...**'), true)
  assert.equal(isStepHeaderLine('Step 40 ...'), true)
  assert.equal(isStepHeaderLine('  **Step 2 ...**  '), true)

  // Ordinary prose must not look like a step.
  assert.equal(isStepHeaderLine('Summary:'), false)
  assert.equal(isStepHeaderLine('Step 2 done'), false)
  assert.equal(isStepHeaderLine('the LLM Running (Step 1) ...'), false)
})

test('countStepHeaders counts one per marker', () => {
  const text = '\n\n**LLM Running (Step 1) ...**\n\nbody\n\n**LLM Running (Step 2) ...**\n\nmore'
  assert.equal(countStepHeaders(text), 2)
  assert.equal(countStepHeaders(''), 0)
  assert.equal(countStepHeaders('no steps here'), 0)
})

test('countNewMessages counts steps, so it grows while a turn is still running', () => {
  // One in-flight turn that has already produced three steps. Counting turns
  // would pin this at 1 for the whole run — the bug being fixed.
  const messages = [
    { role: 'user' as const, text: 'long task' },
    {
      role: 'assistant' as const,
      text: '\n\n**LLM Running (Step 1) ...**\n\na1\n\n**LLM Running (Step 2) ...**\n\na2\n\n**LLM Running (Step 3) ...**\n\na3',
    },
  ]
  assert.equal(countNewMessages(messages, 0), 3)
  // Scrolling away *after* step 1 was already on screen must only count steps 2-3.
  assert.equal(countNewMessages(messages, 1), 3)
})

test('countNewMessages counts each step turn across the whole transcript', () => {
  const messages = [
    { role: 'user' as const, text: 'q1' },
    { role: 'assistant' as const, text: '\n\n**LLM Running (Step 1) ...**\n\na\n\n**LLM Running (Step 2) ...**\n\nb' },
    { role: 'user' as const, text: 'q2' },
    { role: 'assistant' as const, text: '\n\n**LLM Running (Step 1) ...**\n\nc' },
  ]
  // 2 + 1 steps; user messages contribute nothing.
  assert.equal(countNewMessages(messages, 0), 3)
  // Starting at q2 counts only its single step.
  assert.equal(countNewMessages(messages, 2), 1)
})

test('countNewMessages treats a step-less reply as one block', () => {
  const messages = [
    { role: 'user' as const, text: 'q' },
    { role: 'assistant' as const, text: 'plain answer with no step banner' },
  ]
  assert.equal(countNewMessages(messages, 0), 1)
})

test('countNewMessages skips empty assistant placeholders', () => {
  const messages = [
    { role: 'user' as const, text: 'q' },
    { role: 'assistant' as const, text: '' },
    { role: 'assistant' as const, text: '' },
  ]
  assert.equal(countNewMessages(messages, 0), 0)
})

test('countNewMessages clamps an out-of-range start index', () => {
  const messages = [
    { role: 'user' as const, text: 'q' },
    { role: 'assistant' as const, text: '\n\n**Step 1 ...**\n\na' },
  ]
  assert.equal(countNewMessages(messages, -5), 1)
  assert.equal(countNewMessages(messages, 99), 0)
})

test('jumpToBottomLabel pluralises and falls back to the generic label', () => {
  assert.equal(jumpToBottomLabel(0), 'Jump to bottom')
  assert.equal(jumpToBottomLabel(-3), 'Jump to bottom')
  assert.equal(jumpToBottomLabel(1), '1 new message')
  assert.equal(jumpToBottomLabel(2), '2 new messages')
  assert.equal(jumpToBottomLabel(41), '41 new messages')
})

test('jumpToBottomText degrades with the available width', () => {
  // Widest form: clickable, with the arrow.
  const wide = jumpToBottomText({ count: 0, availableColumns: 80, clickable: true })
  assert.equal(wide, 'Jump to bottom (click) ↓')

  // Mid: key hint instead of the click hint.
  const withKey = jumpToBottomText({ count: 0, availableColumns: 40, keyHint: 'ctrl+end' })
  assert.equal(withKey, 'Jump to bottom: ctrl+end to scroll')

  // Narrower: arrow only.
  const narrow = jumpToBottomText({ count: 0, availableColumns: 18 })
  assert.equal(narrow, 'Jump to bottom ↓')

  // Narrowest fallback still keeps a usable label.
  assert.equal(jumpToBottomText({ count: 0, availableColumns: 5 }), 'Jump to bottom')
})

test('jumpToBottomText carries the unread count into the label', () => {
  const text = jumpToBottomText({ count: 3, availableColumns: 80, clickable: true })
  assert.equal(text, '3 new messages (click) ↓')
  assert.equal(jumpToBottomText({ count: 1, availableColumns: 80 }), '1 new message ↓')
})

test('jumpToBottomText never returns an empty string', () => {
  for (const columns of [0, 1, 2, 5, 20]) {
    assert.ok(jumpToBottomText({ count: 0, availableColumns: columns }).length > 0)
  }
})

test('jumpToBottomPlacement centres the pill and includes its padding', () => {
  // "Jump to bottom ↓" is 16 display columns; the pill adds one space each side.
  const placement = jumpToBottomPlacement('Jump to bottom ↓', 40)
  assert.equal(placement.width, 18)
  assert.equal(placement.left, Math.floor((40 - 18) / 2))
  // Symmetric: left offset plus width never exceeds the content area.
  assert.ok(placement.left + placement.width <= 40)
})

test('jumpToBottomPlacement clamps to the content area on a narrow terminal', () => {
  const placement = jumpToBottomPlacement('Jump to bottom (click) ↓', 10)
  assert.equal(placement.left, 0)
  assert.equal(placement.width, 10)
})

test('hitTestJumpToBottom accepts only the pill row and its columns', () => {
  const placement = jumpToBottomPlacement('Jump to bottom ↓', 40)
  const pillRow = 23

  assert.equal(hitTestJumpToBottom(placement, pillRow, pillRow, placement.left), true)
  assert.equal(hitTestJumpToBottom(placement, pillRow, pillRow, placement.left + placement.width - 1), true)
  // Just outside the box on either side.
  assert.equal(hitTestJumpToBottom(placement, pillRow, pillRow, placement.left - 1), false)
  assert.equal(hitTestJumpToBottom(placement, pillRow, pillRow, placement.left + placement.width), false)
  // Wrong row.
  assert.equal(hitTestJumpToBottom(placement, pillRow, pillRow - 1, placement.left), false)
  // No pill on screen.
  assert.equal(hitTestJumpToBottom(null, pillRow, pillRow, placement.left), false)
})
