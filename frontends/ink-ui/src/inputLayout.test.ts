import test from 'node:test'
import assert from 'node:assert/strict'
import { inputChromeSections } from './inputLayout.js'

test('inputChromeSections places slash suggestions below the input frame (Codex popup below composer)', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: false, hasSlashSuggestions: true })

  assert.ok(sections.indexOf('slashSuggestions') > sections.indexOf('input'))
  assert.deepEqual(sections, ['hint', 'input', 'slashSuggestions'])
})

test('inputChromeSections places command panels below the input frame', () => {
  const sections = inputChromeSections({ hasError: false, hasPanel: true, hasSlashSuggestions: false })

  assert.ok(sections.indexOf('panel') > sections.indexOf('input'))
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

test('inputChromeSections prefers panel over slash when both requested', () => {
  assert.deepEqual(inputChromeSections({ hasError: false, hasPanel: true, hasSlashSuggestions: true }), [
    'hint',
    'input',
    'panel',
  ])
})
