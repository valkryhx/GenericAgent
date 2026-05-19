import test from 'node:test'
import assert from 'node:assert/strict'
import { inputChromeSections } from './inputLayout.js'

test('inputChromeSections places slash suggestions below the lower input divider', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: false, hasSlashSuggestions: true })

  assert.ok(sections.indexOf('bottomDivider') < sections.indexOf('slashSuggestions'))
  assert.deepEqual(sections, ['hint', 'topDivider', 'input', 'bottomDivider', 'slashSuggestions'])
})

test('inputChromeSections places slash command panels below the lower input divider', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: true, hasSlashSuggestions: false })

  assert.ok(sections.indexOf('bottomDivider') < sections.indexOf('panel'))
  assert.deepEqual(sections, ['hint', 'topDivider', 'input', 'bottomDivider', 'panel'])
})

test('inputChromeSections keeps errors above the input chrome', () => {
  assert.deepEqual(inputChromeSections({ hasError: true, hasPanel: true, hasSlashSuggestions: false }), [
    'error',
    'hint',
    'topDivider',
    'input',
    'bottomDivider',
    'panel',
  ])
})
