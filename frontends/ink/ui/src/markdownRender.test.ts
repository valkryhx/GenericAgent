import test from 'node:test'
import assert from 'node:assert/strict'
import { renderMarkdownLines, lineText } from './markdownRender.js'

test('renderMarkdownLines styles bold italic and inline code', () => {
  const lines = renderMarkdownLines('Use **bold**, *italic*, and `code`.')

  assert.equal(lineText(lines[0]!), 'Use bold, italic, and code.')
  assert.equal(lines[0]!.parts?.some(part => part.text === 'bold' && part.bold), true)
  assert.equal(lines[0]!.parts?.some(part => part.text === 'italic' && part.italic), true)
  assert.equal(lines[0]!.parts?.some(part => part.text === 'code' && part.color === 'cyan'), true)
})

test('renderMarkdownLines renders headings as emphasized text', () => {
  const lines = renderMarkdownLines('## Result')

  assert.equal(lineText(lines[0]!), 'Result')
  assert.equal(lines[0]!.parts?.[0]?.bold, true)
})

test('renderMarkdownLines preserves fenced code blocks', () => {
  const lines = renderMarkdownLines('```ts\nconst x = 1\n```')

  assert.deepEqual(lines.map(lineText), ['  const x = 1'])
  assert.equal(lines[0]!.parts?.[0]?.color, 'gray')
})

test('renderMarkdownLines normalizes unordered and ordered lists', () => {
  const lines = renderMarkdownLines('- one\n- two\n\n1. first')

  assert.deepEqual(lines.map(lineText), ['- one', '- two', '1. first'])
})

test('renderMarkdownLines keeps GA tool summaries as plain lines', () => {
  const lines = renderMarkdownLines('> code_run(script: print(1))')

  assert.equal(lineText(lines[0]!), '> code_run(script: print(1))')
  assert.equal(lines[0]!.parts?.[0]?.text, '> code_run(script: print(1))')
})

test('renderMarkdownLines bolds LLM Running turn markers', () => {
  const lines = renderMarkdownLines('**LLM Running (Turn 2) ...**\n\nSummary: ok')
  const turn = lines.find(line => lineText(line).includes('LLM Running (Turn 2)'))
  assert.ok(turn)
  assert.equal(lineText(turn!), 'LLM Running (Turn 2) ...')
  assert.equal(turn!.parts?.some(part => part.bold && part.text.includes('LLM Running')), true)
})