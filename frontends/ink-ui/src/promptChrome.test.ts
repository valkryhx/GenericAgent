import test from 'node:test'
import assert from 'node:assert/strict'
import { fixedInputLine, inputContentColumns, inputFrameBorderStyle, inputPrompt, inputPromptLineItems, inputPromptLines, inputVisibleRowCount, renderInputLine } from './promptChrome.js'

test('inputPrompt uses a Claude-style greater-than marker', () => {
  assert.equal(inputPrompt('hello'), '> hello')
  assert.equal(inputPrompt(''), '> ')
})

test('inputPromptLines keeps multiline input and trailing blank cursor rows visible', () => {
  assert.deepEqual(inputPromptLines('hello\n'), ['> hello', '  '])
  assert.deepEqual(inputPromptLines('hello\nworld'), ['> hello', '  world'])
})

test('renderInputLine renders a visible cursor without adding a fake cell', () => {
  const rendered = renderInputLine('> hello', true)

  assert.deepEqual(rendered, [
    { text: '> hell' },
    { text: 'o', inverse: true },
  ])
  assert.equal(rendered.map(part => part.text).join(''), '> hello')
})

test('renderInputLine keeps plain text when cursor is hidden', () => {
  assert.deepEqual(renderInputLine('> hello', false), [{ text: '> hello' }])
})

test('renderInputLine uses the existing prompt space as the empty-input cursor', () => {
  const rendered = renderInputLine('> ', true)

  assert.deepEqual(rendered, [
    { text: '>' },
    { text: ' ', inverse: true },
  ])
  assert.equal(rendered.map(part => part.text).join(''), '> ')
})

test('renderInputLine can render cursor in the middle without changing text width', () => {
  const rendered = renderInputLine('> hello', true, 4)

  assert.deepEqual(rendered, [
    { text: '> h' },
    { text: 'e', inverse: true },
    { text: 'llo' },
  ])
  assert.equal(rendered.map(part => part.text).join(''), '> hello')
})

test('fixedInputLine keeps rendered input rows at a stable width', () => {
  assert.equal(fixedInputLine('> hi', 8), '> hi    ')
  assert.equal(fixedInputLine('> hello world', 8), '> hello ')
  assert.equal(fixedInputLine('> 你好', 8), '> 你好  ')
})

test('inputContentColumns reserves fixed gutter, left padding, and a right-edge cursor cell', () => {
  assert.equal(inputContentColumns(20), 16)
  assert.equal(1 + 2 + inputContentColumns(20), 19)
})

test('inputPromptLineItems uses terminal display width for wide characters', () => {
  assert.deepEqual(inputPromptLineItems('你好', 1, 1), [
    { gutter: '> ', text: '你好', cursorColumn: 2 },
  ])
})

test('renderInputLine keeps wide-character cursor rendering width stable', () => {
  const rendered = renderInputLine('> 你好  ', true, 5)

  assert.deepEqual(rendered, [
    { text: '> 你' },
    { text: '好', inverse: true },
    { text: '  ' },
  ])
  assert.equal(rendered.map(part => part.text).join(''), '> 你好  ')
})

test('inputPromptLineItems keeps the cursor row visible in multiline input', () => {
  assert.deepEqual(inputPromptLineItems('one\ntwo\nthree\nfour', 2, 1), [
    { gutter: '> ', text: 'one', cursorColumn: 1 },
    { gutter: '  ', text: 'two' },
  ])
  assert.deepEqual(inputPromptLineItems('one\ntwo\nthree\nfour', 2, 18), [
    { gutter: '  ', text: 'three' },
    { gutter: '  ', text: 'four', cursorColumn: 4 },
  ])
})

test('inputVisibleRowCount grows for multiline input with a cap', () => {
  assert.equal(inputVisibleRowCount(''), 1)
  assert.equal(inputVisibleRowCount('hello\n'), 2)
  assert.equal(inputVisibleRowCount('1\n2\n3\n4\n5\n6\n7', 4), 4)
})

test('inputFrameBorderStyle draws only horizontal borders with no corner fragments', () => {
  assert.deepEqual(inputFrameBorderStyle, {
    topLeft: '',
    top: '─',
    topRight: '',
    right: '',
    bottomRight: '',
    bottom: '─',
    bottomLeft: '',
    left: '',
  })
})
