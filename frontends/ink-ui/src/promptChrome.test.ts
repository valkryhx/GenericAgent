import test from 'node:test'
import assert from 'node:assert/strict'
import {
  fixedInputLine,
  inputContentColumns,
  inputFrameBorderStyle,
  inputViewport,
  renderInputLine,
} from './promptChrome.js'

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

test('inputViewport soft-wraps one logical line by terminal display width', () => {
  const input = 'abcd你好ef'
  const viewport = inputViewport(input, {
    columns: 6,
    maxRows: 6,
    cursorOffset: input.length,
  })

  assert.deepEqual(viewport.lines.map(line => line.text), ['abcd你', '好ef'])
  assert.deepEqual(viewport.lines.map(line => line.gutter), ['> ', '  '])
  assert.equal(viewport.cursorLine, 1)
  assert.equal(viewport.cursorColumn, 4)
})

test('inputViewport keeps the caret row visible inside a six-row window', () => {
  const input = Array.from({ length: 10 }, (_, index) => `row${index}`).join('\n')
  const viewport = inputViewport(input, { columns: 20, maxRows: 6, cursorOffset: input.length })

  assert.equal(viewport.totalRows, 10)
  assert.equal(viewport.startRow, 4)
  assert.equal(viewport.lines.length, 6)
  assert.equal(viewport.cursorLine, 5)
})

test('inputViewport places a soft-boundary cursor on the following visual row', () => {
  const viewport = inputViewport('abcdefgh', { columns: 4, maxRows: 6, cursorOffset: 4 })

  assert.equal(viewport.cursorLine, 1)
  assert.equal(viewport.cursorColumn, 0)
})

test('inputViewport preserves explicit trailing lines and grapheme boundaries', () => {
  const trailing = inputViewport('hello\n', { columns: 10, cursorOffset: 'hello\n'.length })
  const emoji = inputViewport('👨‍💻x', { columns: 2, cursorOffset: '👨‍💻'.length })

  assert.deepEqual(trailing.lines.map(line => line.text), ['hello', ''])
  assert.equal(trailing.cursorLine, 1)
  assert.equal(trailing.cursorColumn, 0)
  assert.deepEqual(emoji.lines.map(line => line.text), ['👨‍💻', 'x'])
  assert.equal(emoji.cursorLine, 1)
  assert.equal(emoji.cursorColumn, 0)
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
