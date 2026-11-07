import test from 'node:test'
import assert from 'node:assert/strict'
import {
  displayColumnEnd,
  isEmptySelection,
  normalizeSelection,
  partitionByDisplayColumns,
  selectedRowTexts,
  selectedText,
  selectionSpans,
  sliceByDisplayColumn,
} from './transcriptSelection.js'

const ROWS = [
  'alpha one',
  'beta two',
  'gamma three',
  '回答内容在这里',
]

test('normalizeSelection orders points regardless of drag direction', () => {
  const down = normalizeSelection({ row: 0, col: 2 }, { row: 2, col: 5 }, ROWS, 40)
  assert.deepEqual(down, { start: { row: 0, col: 2 }, end: { row: 2, col: 5 } })

  // Same drag, but the user released above/left of where they pressed.
  const up = normalizeSelection({ row: 2, col: 5 }, { row: 0, col: 2 }, ROWS, 40)
  assert.deepEqual(up, { start: { row: 0, col: 2 }, end: { row: 2, col: 5 } })

  // Same row: the column decides, and equal columns keep the anchor first.
  assert.deepEqual(
    normalizeSelection({ row: 1, col: 9 }, { row: 1, col: 4 }, ROWS, 40),
    { start: { row: 1, col: 4 }, end: { row: 1, col: 9 } },
  )
})

test('normalizeSelection clamps out-of-range points instead of dropping them', () => {
  const range = normalizeSelection({ row: -5, col: -3 }, { row: 99, col: 999 }, ROWS, 40)
  assert.deepEqual(range.start, { row: 0, col: 0 })
  assert.deepEqual(range.end, { row: ROWS.length - 1, col: 40 })
})

test('selectedText copies whole rows between the start and end rows', () => {
  const text = selectedText(
    { start: { row: 0, col: 0 }, end: { row: 1, col: 4 } },
    ROWS,
    40,
  )
  assert.equal(text, 'alpha one\nbeta')
})

test('selectedText cuts a single row at both ends', () => {
  const text = selectedText(
    { start: { row: 0, col: 6 }, end: { row: 0, col: 9 } },
    ROWS,
    40,
  )
  assert.equal(text, 'one')
})

test('selectedText never copies past the end of a short row', () => {
  // "beta two" is 8 columns; asking for 0..40 must yield exactly the row.
  const rows = selectedRowTexts(
    { start: { row: 1, col: 0 }, end: { row: 1, col: 40 } },
    ROWS,
    40,
  )
  assert.deepEqual(rows, ['beta two'])
})

test('selectedText slices CJK rows by display column, not string index', () => {
  // Each Han character is 2 display columns and 1 JS code unit. "回答内容在这里"
  // is 7 characters but 14 display columns. Asking for columns 4..8 must
  // therefore yield exactly two characters, not four.
  const rows = ROWS[3]!
  assert.equal(displayColumnEnd(rows), 14)
  assert.equal(sliceByDisplayColumn(rows, 4, 8), '内容')
  const copied = selectedText(
    { start: { row: 3, col: 4 }, end: { row: 3, col: 8 } },
    ROWS,
    40,
  )
  assert.equal(copied, '内容')
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

test('selectionSpans reports half-open column spans per touched row', () => {
  const spans = selectionSpans(
    { start: { row: 0, col: 2 }, end: { row: 2, col: 3 } },
    ROWS,
    40,
  )
  assert.deepEqual(spans[0], { from: 2, to: 9 })
  assert.deepEqual(spans[1], { from: 0, to: 8 })
  assert.deepEqual(spans[2], { from: 0, to: 3 })
  assert.equal(spans[3], undefined)
})

test('selectionSpans is empty for no selection and for a single click', () => {
  assert.deepEqual(selectionSpans(null, ROWS, 40), [undefined, undefined, undefined, undefined])
  const click = selectionSpans({ start: { row: 1, col: 3 }, end: { row: 1, col: 3 } }, ROWS, 40)
  assert.deepEqual(click, [undefined, undefined, undefined, undefined])
})

test('isEmptySelection distinguishes a click from a drag', () => {
  assert.equal(isEmptySelection(null), true)
  assert.equal(isEmptySelection({ start: { row: 1, col: 3 }, end: { row: 1, col: 3 } }), true)
  assert.equal(isEmptySelection({ start: { row: 1, col: 3 }, end: { row: 1, col: 4 } }), false)
  assert.equal(isEmptySelection({ start: { row: 1, col: 3 }, end: { row: 2, col: 0 } }), false)
})
