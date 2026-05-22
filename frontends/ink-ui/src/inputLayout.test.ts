import test from 'node:test'
import assert from 'node:assert/strict'
import { inputChromeSections } from './inputLayout.js'

test('inputChromeSections places slash suggestions above the input frame', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: false, hasSlashSuggestions: true })

  assert.ok(sections.indexOf('slashSuggestions') < sections.indexOf('input'))
  assert.deepEqual(sections, ['slashSuggestions', 'hint', 'input'])
})

test('inputChromeSections places slash command panels above the input frame', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: true, hasSlashSuggestions: false })

  assert.ok(sections.indexOf('panel') < sections.indexOf('input'))
  assert.deepEqual(sections, ['panel', 'hint', 'input'])
})

test('inputChromeSections keeps errors above the input chrome', () => {
  assert.deepEqual(inputChromeSections({ hasError: true, hasPanel: true, hasSlashSuggestions: false }), [
    'error',
    'panel',
    'hint',
    'input',
  ])
})
