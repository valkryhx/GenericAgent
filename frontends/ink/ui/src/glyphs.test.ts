import test from 'node:test'
import assert from 'node:assert/strict'
import { GLYPHS, RESULT_PREFIX, EXPAND_HINT } from './glyphs.js'

test('GLYPHS matches the Claude Code glyph set', () => {
  // Values verified against CC v2.1.283 (m0052 / exe @201043800).
  assert.equal(GLYPHS.resultPrefix, '\u23BF')
  assert.equal(GLYPHS.star, '\u273B')
  assert.equal(GLYPHS.triangleRight, '\u25B8')
  assert.equal(GLYPHS.treeBranch, '\u251C')
  assert.equal(GLYPHS.treeLast, '\u2514')
  assert.equal(GLYPHS.treePipe, '\u2502')
  assert.equal(GLYPHS.rule, '\u2500')
  assert.ok(GLYPHS.bullet === '\u23FA' || GLYPHS.bullet === '\u25CF')
})

test('RESULT_PREFIX and EXPAND_HINT are the CC-rendered literals', () => {
  assert.equal(RESULT_PREFIX, `  ${GLYPHS.resultPrefix}  `)
  assert.equal(EXPAND_HINT, '(ctrl+o to expand)')
})
