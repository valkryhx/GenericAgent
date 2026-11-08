/**
 * Display-column primitives.
 *
 * These are shared by the transcript renderer, the composer renderer and the frame
 * selection model, so they are tested here rather than through any one of them. The
 * selection *model* moved to `frameSelection`/`framePlan` (absolute frame coordinates,
 * so one drag can cross regions) and is tested there.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  displayColumnEnd,
  partitionByDisplayColumns,
  sliceByDisplayColumn,
} from './transcriptSelection.js'

test('sliceByDisplayColumn slices CJK rows by display column, not string index', () => {
  // Each Han character is 2 display columns and 1 JS code unit. "回答内容在这里"
  // is 7 characters but 14 display columns. Asking for columns 4..8 must
  // therefore yield exactly two characters, not four.
  const row = '回答内容在这里'
  assert.equal(displayColumnEnd(row), 14)
  assert.equal(sliceByDisplayColumn(row, 4, 8), '内容')
})

test('sliceByDisplayColumn keeps a wide character that the range cuts through', () => {
  // Columns 3..5 land inside the first Han character (columns 2..4) and inside
  // the second (4..6). Both must be included rather than half-rendered.
  assert.equal(sliceByDisplayColumn('回答内容', 3, 5), '答内')
})

test('partitionByDisplayColumns never duplicates a character at a boundary', () => {
  // The regression: three overlapping slices produced 回答 + 答内 + 内容 = 8 chars
  // from 4. That widened the row on every drag move and made the text jitter.
  const { before, selected, after } = partitionByDisplayColumns('回答内容', 3, 5)
  assert.equal(before + selected + after, '回答内容', 'partition must preserve the text exactly')
  assert.equal(before, '回')
  assert.equal(selected, '答内')
  assert.equal(after, '容')
})

test('partitionByDisplayColumns is lossless for every column pair', () => {
  // Exhaustive over a mixed-width row: this is the property the renderer relies
  // on, so a spot check is not enough.
  const text = 'ab回答cd内容ef'
  const width = displayColumnEnd(text)
  for (let from = 0; from <= width; from += 1) {
    for (let to = from; to <= width; to += 1) {
      const { before, selected, after } = partitionByDisplayColumns(text, from, to)
      assert.equal(
        before + selected + after,
        text,
        `partition(${from}, ${to}) lost or duplicated characters`,
      )
    }
  }
})

test('partitionByDisplayColumns handles an empty selection and full selection', () => {
  const text = 'abc中文'
  // Half-open empty range: everything lands after.
  const empty = partitionByDisplayColumns(text, 2, 2)
  assert.equal(empty.before, 'ab')
  assert.equal(empty.selected, '')
  assert.equal(empty.after, 'c中文')

  // Whole row selected.
  const full = partitionByDisplayColumns(text, 0, displayColumnEnd(text))
  assert.equal(full.before, '')
  assert.equal(full.selected, text)
  assert.equal(full.after, '')
})
