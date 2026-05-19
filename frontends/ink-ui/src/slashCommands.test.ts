import test from 'node:test'
import assert from 'node:assert/strict'
import {
  completeSlashCommand,
  formatSlashDescription,
  formatSlashSuggestionLine,
  shouldCompleteSlashCommand,
  slashSelectionAction,
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

test('slashSuggestions includes matching skills after built-in commands', () => {
  const suggestions = slashSuggestions('/', [
    { name: 'imagegen', description: 'Generate images', source: 'codex', path: 'C:/skills/imagegen/SKILL.md' },
    { name: 'review', description: 'Review code', source: 'claude', path: 'C:/skills/review/SKILL.md' },
  ])

  assert.ok(suggestions.some(command => command.name === '/imagegen' && command.kind === 'skill'))
  assert.equal(slashSuggestions('/im', [
    { name: 'imagegen', description: 'Generate images', source: 'codex', path: 'C:/skills/imagegen/SKILL.md' },
  ])[0].name, '/imagegen')
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

test('formatSlashDescription truncates long skill descriptions with ellipsis', () => {
  const text = 'This is a very long skill description that should not stretch the slash suggestion panel forever.'
  const formatted = formatSlashDescription(text, 42)

  assert.equal(formatted, 'This is a very long skill description t...')
  assert.equal(formatted.length, 42)
  assert.equal(formatSlashDescription('short text', 42), 'short text')
})

test('formatSlashSuggestionLine truncates long skill rows to the brainstorming limit', () => {
  const line = formatSlashSuggestionLine({
    name: '/brainstorming',
    description: 'You MUST use this before any creative work - creating features, building components, adding functionality, or modifying behavior.',
    kind: 'skill',
    source: 'claude',
  })

  assert.equal(line, '/brainstorming [skill: claude] You MUST use this before any creative work - creating f...')
  assert.equal(line.length, 89)
})

test('shouldCompleteSlashCommand does not complete exact commands on Enter', () => {
  const [model] = slashSuggestions('/model')

  assert.equal(shouldCompleteSlashCommand('/model', model), false)
  assert.equal(shouldCompleteSlashCommand('/mod', model), true)
})

test('slashSelectionAction executes built-in commands on Enter and completes skills', () => {
  const [help] = slashSuggestions('/')
  const [imagegen] = slashSuggestions('/im', [
    { name: 'imagegen', description: 'Generate images', source: 'codex', path: 'C:/skills/imagegen/SKILL.md' },
  ])

  assert.deepEqual(slashSelectionAction('/', help, 'enter'), { type: 'execute', value: '/help' })
  assert.deepEqual(slashSelectionAction('/he', help, 'tab'), { type: 'complete', value: '/help ' })
  assert.deepEqual(slashSelectionAction('/im', imagegen, 'enter'), { type: 'complete', value: '/imagegen ' })
})
