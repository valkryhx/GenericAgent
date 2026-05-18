import test from 'node:test'
import assert from 'node:assert/strict'
import {
  completeSlashCommand,
  shouldCompleteSlashCommand,
  moveSlashSelection,
  slashSuggestions,
  visibleSlashSuggestions,
} from './slashCommands.js'

test('slashSuggestions shows all commands for a bare slash', () => {
  const suggestions = slashSuggestions('/')

  assert.ok(suggestions.length > 5)
  assert.equal(suggestions[0].name, '/help')
  assert.ok(suggestions.some(command => command.name === '/resume'))
  assert.ok(suggestions.some(command => command.name === '/rewind'))
})

test('slashSuggestions filters by command prefix only for single-line slash input', () => {
  assert.deepEqual(slashSuggestions('/re').map(command => command.name), ['/resume', '/rewind'])
  assert.deepEqual(slashSuggestions(' /re'), [])
  assert.deepEqual(slashSuggestions('/re\nmore'), [])
})

test('slashSuggestions includes mcp command', () => {
  assert.ok(slashSuggestions('/m').some(command => command.name === '/mcp'))
})

test('slashSuggestions includes model commands', () => {
  assert.ok(slashSuggestions('/m').some(command => command.name === '/model'))
  assert.ok(slashSuggestions('/l').some(command => command.name === '/llm'))
})

test('moveSlashSelection clamps selection inside suggestion bounds', () => {
  const suggestions = slashSuggestions('/')

  assert.equal(moveSlashSelection(0, -1, suggestions), 0)
  assert.equal(moveSlashSelection(0, 2, suggestions), 2)
  assert.equal(moveSlashSelection(99, 1, suggestions), suggestions.length - 1)
})

test('visibleSlashSuggestions returns a five-row scrolling window around selection', () => {
  const suggestions = slashSuggestions('/')
  const first = visibleSlashSuggestions(suggestions, 0)
  const middle = visibleSlashSuggestions(suggestions, 6)

  assert.equal(first.startIndex, 0)
  assert.equal(first.items.length, 5)
  assert.equal(middle.items.length, 5)
  assert.ok(middle.startIndex > 0)
  assert.equal(middle.items[6 - middle.startIndex].name, suggestions[6].name)
})

test('completeSlashCommand inserts selected command with a trailing space', () => {
  const [resume] = slashSuggestions('/res')

  assert.equal(completeSlashCommand(resume), '/resume ')
})

test('shouldCompleteSlashCommand does not complete exact commands on Enter', () => {
  const [model] = slashSuggestions('/model')

  assert.equal(shouldCompleteSlashCommand('/model', model), false)
  assert.equal(shouldCompleteSlashCommand('/mod', model), true)
})
