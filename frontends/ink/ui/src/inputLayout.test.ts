import test from 'node:test'
import assert from 'node:assert/strict'
import { inputChromeSections } from './inputLayout.js'

test('inputChromeSections places the hint under the input and slash suggestions below both', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: false, hasSlashSuggestions: true })

  assert.ok(sections.indexOf('slashSuggestions') > sections.indexOf('input'))
  assert.deepEqual(sections, ['input', 'hint', 'slashSuggestions'])
})

test('inputChromeSections places command panels below the input frame', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: true, hasSlashSuggestions: false })

  assert.ok(sections.indexOf('panel') > sections.indexOf('input'))
  assert.deepEqual(sections, ['input', 'hint', 'panel'])
})

test('inputChromeSections places live MCP startup status below the composer', () => {
  assert.deepEqual(inputChromeSections({
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
    hasMcpStatus: true,
  }), ['input', 'hint', 'mcpStatus'])
})

test('inputChromeSections keeps errors above the input chrome', () => {
  assert.deepEqual(inputChromeSections({ hasError: true, hasPanel: true, hasSlashSuggestions: false }), [
    'error',
    'input',
    'hint',
    'panel',
  ])
})

test('inputChromeSections prefers panel over slash when both requested', () => {
  assert.deepEqual(inputChromeSections({ hasError: false, hasPanel: true, hasSlashSuggestions: true }), [
    'input',
    'hint',
    'panel',
  ])
})

test('inputChromeSections keeps temporary status text above the input', () => {
  assert.deepEqual(inputChromeSections({
    hasError: false,
    hasPanel: false,
    hasSlashSuggestions: false,
    hasAboveInputStatus: true,
  }), ['hint', 'input'])
})
