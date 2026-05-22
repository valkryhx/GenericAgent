import test from 'node:test'
import assert from 'node:assert/strict'
import { inputFrameBorderStyle, inputPrompt, inputPromptLineItems, inputPromptLines, inputVisibleRowCount, renderInputLine } from './promptChrome.js'

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

test('inputPromptLineItems keeps the cursor row visible in multiline input', () => {
  assert.deepEqual(inputPromptLineItems('one\ntwo\nthree\nfour', 2, 1), [
    { text: '> one', cursorColumn: 3 },
    { text: '  two' },
  ])
  assert.deepEqual(inputPromptLineItems('one\ntwo\nthree\nfour', 2, 18), [
    { text: '  three' },
    { text: '  four', cursorColumn: 6 },
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
