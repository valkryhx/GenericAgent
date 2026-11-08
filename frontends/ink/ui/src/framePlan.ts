/**
 * Frame assembly: which absolute screen row does each region occupy?
 *
 * `frameSelection` can turn two absolute screen points into text, but only if it is
 * handed a frame that matches what is actually painted. The reported bug ("I cannot
 * drag-select anything in the input box") was not a wrong highlight -- the composer had
 * no address in the selection model at all, since the model covered only the transcript
 * viewport. So the fix lives in this assembly, and its correctness decides whether the
 * whole feature works.
 *
 * Where the composer is
 * ---------------------
 * The composer's row is *not* re-derived here from the layout constants. It is passed in
 * from `inputCursorPosition`, the same value the caret is parked at every frame. That is
 * deliberate: the caret is the one piece of the geometry the user physically verifies
 * (they can see it land in the input box), so anchoring to it means the selection can be
 * off only in ways that already broke the caret. Re-deriving the row from
 * header/band/composer constants would make this module right by its own arithmetic even
 * when the painted layout disagrees -- the exact class of false pass this repo has been
 * burned by.
 *
 * Rows above and below are filled best-effort: the transcript gets its real viewport
 * height, so its row indices are exact; the band's *text* is best-effort, but its row
 * count is derived from the anchored composer row rather than guessed, so a slightly
 * wrong band never moves the composer.
 */

import { composerFrameRows, type FrameRow } from './frameSelection.js'
import { transcriptContentColumns } from './layoutMetrics.js'

export type FramePlanInput = {
  /** Canvas width (`terminalCanvasColumns`) -- the frame's own coordinate space. */
  canvasColumns: number
  /** Rows the layout budget gives the whole frame. */
  rows: number
  /** Header band height (0 when the mode has no header). */
  headerRows: number
  /** Header text; only the first header row is filled with it. */
  header?: string | null
  /** Rows the transcript viewport occupies, whatever it is filled with. */
  transcriptRows: number
  /** Visible transcript row texts, top to bottom. */
  transcriptLines: string[]
  /** Absolute row of the composer's first painted line (from `inputCursorPosition`). */
  composerRow: number
  /** Composer lines, already split into gutter + text. */
  composerLines: Array<{ gutter: string; text: string }>
  /**
   * Text of the band between the transcript and the composer (goal card, activity,
   * notices). Best-effort: the *count* of band rows comes from `composerRow`, so a
   * mismatch here pads or trims this text only.
   */
  bandLines?: string[]
  /** Text below the composer (hint, panels), in paint order. Best-effort. */
  belowLines?: string[]
}

/** The transcript's left padding, as painted by `MessageViewport`. */
const TRANSCRIPT_GUTTER = 1
/** Bottom-chrome sections paint with `paddingX={1}`. */
const CHROME_GUTTER = 1

function put(rows: FrameRow[], index: number, text: string, leftGutter: number, columns: number): void {
  if (index < 0 || index >= rows.length) return
  rows[index] = { text, leftGutter, columns }
}

function fill(
  rows: FrameRow[],
  from: number,
  to: number,
  lines: string[],
  leftGutter: number,
  columns: number,
): void {
  for (let index = from; index < to; index += 1) {
    put(rows, index, lines[index - from] ?? '', leftGutter, columns)
  }
}

/**
 * Assemble the frame rows.
 *
 * The composer is placed at `composerRow`, the transcript is placed directly under the
 * header, and everything else is filler. Total height is `rows` unless the anchored
 * composer would fall outside it, in which case it grows to keep the composer
 * addressable -- a clipped composer would be the one row the user is most likely to
 * drag.
 */
export function buildFramePlan(input: FramePlanInput): FrameRow[] {
  const canvas = Math.max(1, Math.floor(input.canvasColumns))
  const transcriptColumns = transcriptContentColumns(canvas)
  const chromeColumns = Math.max(1, canvas - 2)
  const headerRows = Math.max(0, Math.floor(input.headerRows))
  const transcriptRows = Math.max(0, Math.floor(input.transcriptRows))
  const composerRows = input.composerLines.length
  const composerRow = Math.max(0, Math.floor(input.composerRow))
  const total = Math.max(Math.floor(input.rows), headerRows + transcriptRows, composerRow + composerRows)

  const rows: FrameRow[] = new Array(total).fill(null).map(() => ({
    text: '',
    leftGutter: CHROME_GUTTER,
    columns: chromeColumns,
  }))

  // Header band: only the first row carries text (the title/status line).
  fill(rows, 0, headerRows, [input.header ?? ''], CHROME_GUTTER, chromeColumns)

  // Transcript: exact height, so its row indices are exact.
  const transcriptStart = headerRows
  const transcriptEnd = transcriptStart + transcriptRows
  fill(rows, transcriptStart, transcriptEnd, input.transcriptLines, TRANSCRIPT_GUTTER, transcriptColumns)

  // Band: row count derived from the anchored composer row, not guessed.
  fill(rows, transcriptEnd, composerRow, input.bandLines ?? [], CHROME_GUTTER, chromeColumns)

  // Composer: the '> ' gutter is painted, so it belongs to the row text.
  const composerFrame = composerFrameRows(input.composerLines, canvas)
  for (let index = 0; index < composerFrame.length; index += 1) {
    const row = composerFrame[index]
    if (row) put(rows, composerRow + index, row.text, row.leftGutter, row.columns)
  }

  // Below the composer: hint and panels.
  fill(rows, composerRow + composerRows, total, input.belowLines ?? [], CHROME_GUTTER, chromeColumns)

  return rows
}
