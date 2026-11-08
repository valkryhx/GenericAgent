/**
 * Frame assembly: which absolute screen row is each region on?
 *
 * `frameSelection` knows how to turn two absolute points into text, but it has to be
 * handed a frame that matches what is painted. This module builds that frame.
 *
 * The bugs these tests guard
 * --------------------------
 * 1. The composer had no address at all (only the transcript viewport did), so every
 *    drag inside the input box clamped to the last transcript row.
 * 2. The composer's row is taken from `inputCursorPosition` -- the same anchor the caret
 *    is parked at -- rather than re-derived from layout constants. A test that let this
 *    module compute the row from its own arithmetic would stay green while the painted
 *    layout disagreed, which is the false-pass shape this repo has hit before. So one
 *    test anchors deliberately off-grid and asserts the composer lands there anyway.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { buildFramePlan } from './framePlan.js'
import { normalizeFrameSelection, selectedFrameText } from './frameSelection.js'
import { transcriptContentColumns } from './layoutMetrics.js'
import { inputContentColumns, inputGutterColumns } from './promptChrome.js'

const CANVAS = 40

function plan(overrides: Partial<Parameters<typeof buildFramePlan>[0]> = {}) {
  return buildFramePlan({
    canvasColumns: CANVAS,
    rows: 12,
    headerRows: 1,
    header: 'GenericAgent            running',
    transcriptRows: 3,
    transcriptLines: ['alpha one', 'beta two', 'gamma three'],
    // header(1) + transcript(3) + band(1) => composer starts at row 5.
    composerRow: 5,
    composerLines: [{ gutter: '> ', text: 'hello world' }],
    bandLines: ['✻ Working (3s)'],
    belowLines: ['Enter send · Alt+Enter newline'],
    ...overrides,
  })
}

test('each region lands on the row the layout puts it on', () => {
  const frame = plan()
  assert.equal(frame.length, 12, 'the frame must match the layout row budget exactly')
  assert.equal(frame[0]!.text.startsWith('GenericAgent'), true)
  assert.deepEqual(frame.slice(1, 4).map(row => row.text), ['alpha one', 'beta two', 'gamma three'])
  assert.equal(frame[4]!.text, '✻ Working (3s)')
  assert.equal(frame[5]!.text, '> hello world')
  assert.equal(frame[6]!.text, 'Enter send · Alt+Enter newline')
})

test('the composer goes where the caret anchor says, not where layout arithmetic would', () => {
  // The caret is the one piece of geometry the user physically verifies, so it is the
  // authority. Deliberately anchor off the "expected" row: if this module re-derived the
  // row from header/transcript/band counts it would put the composer at 5 and this test
  // would fail -- correctly, because then a real layout change could silently move the
  // painted composer away from the selectable one.
  const frame = plan({ composerRow: 7 })
  assert.equal(frame[5]!.text, '') // band padding absorbed the difference
  assert.equal(frame[7]!.text, '> hello world')
  const range = normalizeFrameSelection({ row: 7, col: 3 }, { row: 7, col: 8 }, frame)
  assert.equal(selectedFrameText(range, frame), 'hello')
})

test('a drag inside the composer selects the composer text, not the transcript', () => {
  const frame = plan()
  // Press on the 'h' of "hello": canvas column 3 (1 column of box padding + 2 of gutter).
  const range = normalizeFrameSelection({ row: 5, col: 3 }, { row: 5, col: 8 }, frame)
  assert.equal(selectedFrameText(range, frame), 'hello')
})

test('a drag from the last transcript row into the composer carries both regions', () => {
  const frame = plan()
  const range = normalizeFrameSelection({ row: 3, col: 1 }, { row: 5, col: 7 }, frame)
  assert.deepEqual(selectedFrameText(range, frame).split('\n'), [
    'gamma three',
    '✻ Working (3s)',
    '> hell',
  ])
})

test('a short transcript is padded so the chrome keeps its absolute rows', () => {
  // The viewport has a fixed height. If the pad were skipped, the composer would sit
  // higher in the model than on screen and every drag would be off by one.
  const frame = plan({ transcriptLines: ['only one'] })
  assert.deepEqual(frame.slice(1, 4).map(row => row.text), ['only one', '', ''])
  assert.equal(frame[5]!.text, '> hello world')
})

test('a long transcript is clipped at the viewport height, never pushed into the composer', () => {
  const frame = plan({ transcriptLines: ['1', '2', '3', '4', '5'] })
  assert.deepEqual(frame.slice(1, 4).map(row => row.text), ['1', '2', '3'])
  assert.equal(frame[5]!.text, '> hello world')
})

test('the composer carries its own gutter and width, not the transcript\'s', () => {
  const frame = plan()
  const composer = frame[5]!
  const transcript = frame[1]!
  assert.equal(composer.leftGutter, 1)
  // The regions wrap at genuinely different widths (the composer spends 2 columns on its
  // '> ' gutter and reserves one on the right). One global width would clamp one
  // region's rows at the other region's edge.
  assert.notEqual(composer.columns, transcript.columns)
  assert.equal(composer.columns, inputGutterColumns + inputContentColumns(CANVAS))
  assert.equal(transcript.columns, transcriptContentColumns(CANVAS))
})

test('no header band means the transcript starts at row 0', () => {
  const frame = buildFramePlan({
    canvasColumns: CANVAS,
    rows: 6,
    headerRows: 0,
    transcriptRows: 2,
    transcriptLines: ['alpha one'],
    composerRow: 2,
    composerLines: [{ gutter: '> ', text: 'hi' }],
  })
  assert.equal(frame[0]!.text, 'alpha one')
  assert.equal(frame[2]!.text, '> hi')
})

test('a multi-line composer keeps every row addressable', () => {
  const frame = plan({
    composerRow: 4,
    composerLines: [{ gutter: '> ', text: 'first' }, { gutter: '  ', text: 'second' }],
    bandLines: [],
  })
  assert.equal(frame[4]!.text, '> first')
  assert.equal(frame[5]!.text, '  second')
  const range = normalizeFrameSelection({ row: 5, col: 3 }, { row: 5, col: 9 }, frame)
  assert.equal(selectedFrameText(range, frame), 'second')
})

test('a composer anchored past the row budget still gets its rows', () => {
  // Growing is better than clipping: a clipped composer is the one row the user is most
  // likely to aim at, and a missing row silently clamps the drag to whatever is above.
  const frame = plan({ rows: 6, composerRow: 8 })
  assert.ok(frame.length > 8)
  assert.equal(frame[8]!.text, '> hello world')
})
