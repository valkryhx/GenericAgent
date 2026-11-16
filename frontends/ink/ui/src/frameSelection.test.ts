/**
 * Whole-frame text selection.
 *
 * The reported bug: dragging inside the composer selected nothing (or, worse, the
 * last transcript row), because the selection model only knew about the transcript
 * viewport and clamped every point into it. Claude Code does not have a "transcript
 * selection" — its mouse selection is anchored on *screen* coordinates and reads the
 * rendered frame (`m0701: handleSelectionStart/handleSelectionDrag`, text taken from
 * `this.frontFrame.screen`), so a drag can start in the transcript and end in the
 * composer, or live entirely inside the composer.
 *
 * GA has no screen buffer to read back, so the equivalent is a *frame row model*:
 * every rendered row in order, each carrying its own left gutter and content width
 * (the transcript and the composer use different widths). Row indices are absolute
 * frame rows and columns are absolute canvas columns, which is what makes one drag
 * span regions.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  buildFrameRows,
  composerFrameRows,
  fitFrameRows,
  framePointFromMouse,
  frameSelectionSpans,
  isEmptyFrameSelection,
  normalizeFrameSelection,
  selectedFrameRowTexts,
  selectedFrameText,
  type FrameRow,
} from './frameSelection.js'
import { partitionByDisplayColumns } from './transcriptSelection.js'
import { buildFramePlan } from './framePlan.js'
import { inputContentColumns, inputGutterColumns } from './promptChrome.js'

/** A realistic fullscreen frame: header, transcript, activity band, composer, hint. */
const FRAME: FrameRow[] = [
  { text: 'GenericAgent running', leftGutter: 0, columns: 60 }, // 0 header
  { text: 'alpha one', leftGutter: 1, columns: 40 }, // 1 transcript
  { text: 'beta two', leftGutter: 1, columns: 40 }, // 2 transcript
  { text: '', leftGutter: 1, columns: 40 }, // 3 activity band (spinner row)
  { text: '> hello world', leftGutter: 1, columns: 42 }, // 4 composer
  { text: 'Enter send · Alt+Enter newline', leftGutter: 1, columns: 40 }, // 5 hint
]

test('framePointFromMouse turns a 1-based SGR report into a 0-based frame point', () => {
  // SGR reports are 1-based on both axes and are *absolute screen* coordinates --
  // no header offset, no gutter offset. Those belong to the row model, not the point.
  assert.deepEqual(framePointFromMouse(4, 6), { row: 5, col: 3 })
  assert.deepEqual(framePointFromMouse(1, 1), { row: 0, col: 0 })
})

test('normalizeFrameSelection orders points regardless of drag direction', () => {
  const down = normalizeFrameSelection({ row: 1, col: 2 }, { row: 4, col: 9 }, FRAME)
  assert.deepEqual(down, { start: { row: 1, col: 2 }, end: { row: 4, col: 9 } })

  // Same drag released above/left of where it was pressed.
  const up = normalizeFrameSelection({ row: 4, col: 9 }, { row: 1, col: 2 }, FRAME)
  assert.deepEqual(up, { start: { row: 1, col: 2 }, end: { row: 4, col: 9 } })

  // Same row: the column decides, and equal columns keep the anchor first.
  assert.deepEqual(
    normalizeFrameSelection({ row: 2, col: 9 }, { row: 2, col: 4 }, FRAME),
    { start: { row: 2, col: 4 }, end: { row: 2, col: 9 } },
  )
})

test('normalizeFrameSelection clamps into the frame instead of dropping the selection', () => {
  const range = normalizeFrameSelection({ row: -5, col: -3 }, { row: 99, col: 999 }, FRAME)
  assert.deepEqual(range.start, { row: 0, col: 0 })
  // The clamp is to the row's *allocated* width, not to its text end. Clamping to the
  // text was the "last line is hard to select, aim below it instead" bug: a short row
  // only accepted clicks inside its few characters, so aiming at the line's end landed
  // past it and was dropped. A point past a row's text is still that row's point.
  // Extraction is unaffected — it slices each row by that row's own text length.
  const lastRow = FRAME[FRAME.length - 1]!
  assert.deepEqual(range.end, { row: FRAME.length - 1, col: lastRow.leftGutter + lastRow.columns })
  assert.ok(range.end.col > 31, `列上限应是整行宽度: ${JSON.stringify(range.end)}`)
})

test('a drag that starts in the composer selects the composer text', () => {
  // THE reported bug. Press on the "h" of "hello" (canvas column 3: column 1 is the
  // box padding, column 2 the composer gutter '>', column 3 the first character) and
  // drag to column 8, staying inside the composer the whole way.
  const range = normalizeFrameSelection({ row: 4, col: 3 }, { row: 4, col: 8 }, FRAME)
  assert.equal(selectedFrameText(range, FRAME), 'hello')
})

test('a drag from the transcript down into the composer carries both regions', () => {
  const range = normalizeFrameSelection({ row: 1, col: 1 }, { row: 4, col: 12 }, FRAME)
  assert.deepEqual(selectedFrameRowTexts(range, FRAME), [
    'alpha one', // start row: from its first content column to the row end
    'beta two', // fully inside the selection
    '', // activity band
    '> hello wor', // end row: cut at the drag column
  ])
})

test('a drag starting inside the left gutter begins at the first character', () => {
  // The transcript paints its text one column in, so canvas column 0 is the gutter, not
  // the first character. A drag that starts there must select from the first character --
  // not from a negative offset, and not skipping it.
  const frame: FrameRow[] = [{ text: 'alpha one', leftGutter: 1, columns: 40 }]
  // "alpha one" is 9 columns; with a 1-column gutter it occupies absolute columns 1..10.
  const whole = normalizeFrameSelection({ row: 0, col: 0 }, { row: 0, col: 10 }, frame)
  assert.equal(selectedFrameText(whole, frame), 'alpha one')
  // Starting at the gutter and ending at the gutter still gives the first character,
  // because the end clamps to a real column, not to zero.
  const firstChar = normalizeFrameSelection({ row: 0, col: 0 }, { row: 0, col: 2 }, frame)
  assert.equal(selectedFrameText(firstChar, frame), 'a')
})

test('a composer row copies its text without any trailing padding', () => {
  // The renderer paints `fixedInputLine`, i.e. the row is padded with spaces so the caret
  // box has somewhere to sit. The frame model deliberately stores the *unpadded* text --
  // that is what a user means by "copy this line" -- so a drag well past the end of the
  // text still copies only the text.
  const rows = composerFrameRows([{ gutter: '> ', text: 'hi' }], 40)
  const range = normalizeFrameSelection({ row: 0, col: 0 }, { row: 0, col: 39 }, rows)
  assert.equal(selectedFrameText(range, rows), '> hi')
})

test('composer rows keep the gutter and their own content width', () => {
  const canvasColumns = 40
  const rows = composerFrameRows(
    [
      { gutter: '> ', text: 'hi there' },
      { gutter: '  ', text: 'second line' },
    ],
    canvasColumns,
  )
  assert.equal(rows.length, 2)
  assert.equal(rows[0]!.text, '> hi there')
  assert.equal(rows[1]!.text, '  second line')
  // The composer starts after the box's left padding, and its usable width is the
  // gutter plus the wrapped content width -- not the transcript's width.
  assert.equal(rows[0]!.leftGutter, 1)
  assert.equal(rows[0]!.columns, inputGutterColumns + inputContentColumns(canvasColumns))
})

test('a highlight spanning a CJK composer row reproduces the row exactly', () => {
  const rows = composerFrameRows([{ gutter: '> ', text: '回答内容在这里' }], 40)
  const range = normalizeFrameSelection({ row: 0, col: 1 }, { row: 0, col: 8 }, rows)
  const span = frameSelectionSpans(range, rows)[0]
  assert.ok(span)
  const parts = partitionByDisplayColumns(rows[0]!.text, span.from, span.to)
  // Concatenating the three pieces must give back the row unchanged: a wide character
  // that straddles a boundary must land in exactly one bucket. `回答内容` highlighted
  // at columns 3..5 used to produce 8 characters out of 4 and the row grew.
  assert.equal(parts.before + parts.selected + parts.after, rows[0]!.text)
  assert.ok(parts.selected.length > 0)
})

test('frameSelectionSpans is aligned with the frame rows and offsets by the row gutter', () => {
  const range = normalizeFrameSelection({ row: 4, col: 1 }, { row: 4, col: 4 }, FRAME)
  const spans = frameSelectionSpans(range, FRAME)
  assert.equal(spans.length, FRAME.length)
  for (const index of [0, 1, 2, 3, 5]) assert.equal(spans[index], undefined)
  // Columns are absolute, spans are relative to the row text: column 1 is the gutter,
  // so the span starts at 0 and covers the gutter plus the first three characters.
  assert.deepEqual(spans[4], { from: 0, to: 3 })
})

test('buildFrameRows concatenates sections in render order', () => {
  const rows = buildFrameRows([
    { lines: ['header'], leftGutter: 0, columns: 20 },
    { blankRows: 2, leftGutter: 1, columns: 20 },
    { lines: ['> typed'], leftGutter: 1, columns: 20 },
  ])
  assert.deepEqual(rows.map(row => row.text), ['header', '', '', '> typed'])
  assert.deepEqual(rows.map(row => row.leftGutter), [0, 1, 1, 1])
})

test('fitFrameRows pads or truncates the tail so the frame matches the layout budget', () => {
  // The bottom band is a fixed-height box: too few rows leaves blanks at the bottom,
  // too many are clipped at the bottom. Either way the composer must keep its index.
  const rows = buildFrameRows([{ lines: ['a', 'b'], leftGutter: 0, columns: 10 }])
  const padded = fitFrameRows(rows, 4, 10)
  assert.deepEqual(padded.map(row => row.text), ['a', 'b', '', ''])
  const clipped = fitFrameRows(rows, 1, 10)
  assert.deepEqual(clipped.map(row => row.text), ['a'])
})

test('a click without a drag selects nothing worth copying', () => {
  const range = normalizeFrameSelection({ row: 4, col: 3 }, { row: 4, col: 3 }, FRAME)
  assert.equal(isEmptyFrameSelection(range), true)
})

test('a click without a drag paints no highlight either', () => {
  // Copying nothing is only half the contract: a one-cell inverse block left behind after
  // a click looks like a rendering glitch, so the highlight must be empty too.
  const click = normalizeFrameSelection({ row: 4, col: 3 }, { row: 4, col: 3 }, FRAME)
  assert.deepEqual(frameSelectionSpans(click, FRAME), FRAME.map(() => undefined))
  // A real drag on the same row still paints.
  const drag = normalizeFrameSelection({ row: 4, col: 3 }, { row: 4, col: 6 }, FRAME)
  assert.deepEqual(frameSelectionSpans(drag, FRAME)[4], { from: 2, to: 5 })
})

// ── 短行的可选列范围 ──────────────────────────────────────────────────
//
// 回归：`rowEndColumn` 原本把列上限裁到**文本宽度**，于是只有三个字的末行只接受
// 三列内的点击。用户想「选到最后一行末尾」时会落到文字右侧，那个点被丢弃，
// 表现为「最后一行很难选中，必须在它下方而不是末尾点」。
//
// 修正后列上限是整行分配宽度。取文本仍按各行自身文本切片，所以多出来的列
// 不产生任何字符。

function frameOf(lines: string[]) {
  return buildFramePlan({
    canvasColumns: 80,
    rows: 12,
    headerRows: 1,
    header: 'H',
    transcriptRows: 5,
    transcriptLines: lines,
    composerRow: 7,
    composerLines: [{ gutter: '> ', text: '' }],
    bandLines: [''],
    belowLines: ['hint'],
  })
}

test('a click far right of a short row still ends the selection on that row', () => {
  const frame = frameOf(['短', '中等长度的一行', '末'])
  // 末行只有 1 个字；点在它右侧 70 列外
  const range = normalizeFrameSelection({ row: 1, col: 0 }, { row: 3, col: 75 }, frame)
  assert.equal(range.end.row, 3, `末行右侧的点击应留在末行: ${JSON.stringify(range.end)}`)
  assert.equal(selectedFrameText(range, frame), '短\n中等长度的一行\n末')
})

test('trailing columns past a row\'s text add no characters', () => {
  const frame = frameOf(['abc', 'de'])
  const range = normalizeFrameSelection({ row: 1, col: 0 }, { row: 2, col: 79 }, frame)
  assert.equal(selectedFrameText(range, frame), 'abc\nde')
})

test('an empty filler row is a reachable selection endpoint', () => {
  // 视口下方常有多条空白填充行；它们以前 col 上限是 1，几乎点不中。
  const frame = frameOf(['内容'])
  const range = normalizeFrameSelection({ row: 1, col: 0 }, { row: 4, col: 40 }, frame)
  assert.equal(range.end.row, 4)
  // 列上限是整行宽度，不是 0（旧行为：空文本 → 上限 1）。
  assert.ok(range.end.col > 20, `空白行的列上限应为整行宽度: ${JSON.stringify(range.end)}`)
})

test('selection through a short final line keeps every line', () => {
  const frame = frameOf(['第一行', '第二行', '末'])
  const range = normalizeFrameSelection({ row: 1, col: 0 }, { row: 3, col: 79 }, frame)
  const text = selectedFrameText(range, frame)
  assert.ok(text.includes('第一行'), text)
  assert.ok(text.includes('第二行'), text)
  assert.ok(text.trimEnd().endsWith('末'), `末行丢失: ${JSON.stringify(text)}`)
})
