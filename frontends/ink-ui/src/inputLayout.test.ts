import test from 'node:test'
import assert from 'node:assert/strict'
import { inputChromeSections } from './inputLayout.js'

test('inputChromeSections places slash suggestions below the input frame', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: false, hasSlashSuggestions: true })

  assert.ok(sections.indexOf('input') < sections.indexOf('slashSuggestions'))
  assert.deepEqual(sections, ['hint', 'input', 'slashSuggestions'])
})

test('inputChromeSections places slash command panels below the input frame', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: true, hasSlashSuggestions: false })

  assert.ok(sections.indexOf('input') < sections.indexOf('panel'))
  assert.deepEqual(sections, ['hint', 'input', 'panel'])
})

test('inputChromeSections keeps errors above the input chrome', () => {
  assert.deepEqual(inputChromeSections({ hasError: true, hasPanel: true, hasSlashSuggestions: false }), [
    'error',
    'hint',
    'input',
    'panel',
  ])
})
