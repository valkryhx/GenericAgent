import { test } from 'node:test'
import assert from 'node:assert/strict'
import { contextUsageHint } from './contextHint.js'

test('contextUsageHint is silent at ok', () => {
  assert.equal(contextUsageHint({ level: 'ok', used_tokens: 10, limit_tokens: 100 }), null)
})

test('contextUsageHint shows the percentage and the level', () => {
  assert.equal(
    contextUsageHint({ level: 'warn', used_tokens: 93_000, limit_tokens: 100_000 }),
    'Context 93% · auto-compact coming up',
  )
  assert.equal(
    contextUsageHint({ level: 'compact', used_tokens: 100_000, limit_tokens: 100_000 }),
    'Context 100% · auto-compact on the next turn',
  )
  assert.equal(
    contextUsageHint({ level: 'blocked', used_tokens: 120_000, limit_tokens: 100_000 }),
    'Context 100% · next request may be rejected',
  )
})

test('contextUsageHint ignores a zero limit', () => {
  assert.equal(contextUsageHint({ level: 'warn', used_tokens: 10, limit_tokens: 0 }), null)
})
