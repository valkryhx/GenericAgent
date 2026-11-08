/**
 * Whole-frame text selection.
 *
 * Why this exists: taking the mouse (needed so the wheel can page the transcript
 * instead of being turned into arrow keys by DECSET 1007) costs the terminal's own
 * click-drag selection. GA paid that price and drew its own highlight, but only over
 * the *transcript viewport* -- so a drag inside the composer selected nothing, and
 * dragging from the transcript down into the composer silently clamped to the last
 * transcript row.
 *
 * Claude Code has no "transcript selection": `onSelectionStart` / `onSelectionDrag`
 * receive absolute screen coordinates and the copied text comes out of
 * `this.frontFrame.screen`, the whole rendered frame (319 `m0701`, mouse handler at
 * ~11870-11925). One drag can therefore begin in the transcript and end in the
 * composer, or never leave the composer.
 *
 * GA has no screen buffer to read back, so the equivalent is a *frame row model*:
 * every rendered row, in render order, each carrying its own left gutter and content
 * width -- the transcript and the composer use different widths, so a single global
 * width would mis-slice one of them. Points are absolute frame coordinates
 * (row = absolute screen row, col = absolute canvas column), which is what lets one
 * selection span regions. Each helper below then translates into the row's own
 * coordinates, where the renderer needs them.
 */

import stringWidth from 'string-width'
import { inputContentColumns, inputGutterColumns, inputLeftPaddingColumns } from './promptChrome.js'
import { sliceByDisplayColumn, type SelectionSpan } from './transcriptSelection.js'

/** One rendered frame row. */
export type FrameRow = {
  /** The row's text, as painted, including its own gutter/prefix. */
  text: string
  /** Canvas column the row's text starts at (transcript gutter, composer padding). */
  leftGutter: number
  /** Display width reserved for this row's content, after `leftGutter`. */
  columns: number
}

export type FramePoint = {
  /** Zero-based absolute frame row (0 = topmost rendered row). */
  row: number
  /** Zero-based absolute canvas column (0 = left edge of the canvas). */
  col: number
}

export type FrameSelectionRange = {
  start: FramePoint
  end: FramePoint
}

/**
 * Translate an SGR mouse report into an absolute frame point.
 *
 * SGR reports are 1-based on both axes. Nothing else is subtracted here: the header
 * rows and the left gutter are properties of the *rows*, so they are applied when a
 * row is built rather than when a point is decoded. That is what keeps a point
 * meaningful across regions that have different gutters.
 */
export function framePointFromMouse(x: number, y: number): FramePoint {
  return { row: Math.floor(y) - 1, col: Math.floor(x) - 1 }
}

/** Absolute column at which a row's content ends. */
function rowEndColumn(row: FrameRow): number {
  return row.leftGutter + Math.min(displayWidth(row.text), Math.max(1, row.columns))
}

function clampPoint(point: FramePoint, frame: FrameRow[]): FramePoint {
  if (frame.length === 0) return { row: 0, col: 0 }
  const row = Math.min(frame.length - 1, Math.max(0, Math.floor(point.row)))
  const target = frame[row]!
  const col = Math.min(rowEndColumn(target), Math.max(0, Math.floor(point.col)))
  return { row, col }
}

/** Order two points so `start` is always before `end`, regardless of drag direction. */
export function normalizeFrameSelection(
  anchor: FramePoint,
  focus: FramePoint,
  frame: FrameRow[],
): FrameSelectionRange {
  const a = clampPoint(anchor, frame)
  const b = clampPoint(focus, frame)
  const aFirst = a.row < b.row || (a.row === b.row && a.col <= b.col)
  return aFirst ? { start: a, end: b } : { start: b, end: a }
}

/**
 * Convert an absolute column span on one row into row-relative columns, or `null`
 * when the span does not touch the row's text at all.
 *
 * The left gutter is excluded (a drag that starts left of the text begins at its
 * first character) and the end is clamped to the text's own width, never to the
 * reserved width -- the composer pads every line out to the full content width so the
 * caret has somewhere to sit, and copying those padding spaces would append a run of
 * blanks to every line that was dragged past its end.
 */
function relativeSpan(row: FrameRow, fromCol: number, toCol: number): SelectionSpan | null {
  const contentStart = row.leftGutter
  const from = Math.max(contentStart, fromCol)
  const to = Math.min(rowEndColumn(row), toCol)
  if (to < from) return null
  return { from: from - contentStart, to: to - contentStart }
}

/**
 * The selected text, one entry per frame row the selection touches.
 *
 * Rows the selection passes over entirely contribute their full text; the first and
 * last rows are cut at the drag edges. Rows with no content (the blank activity band,
 * or a wholly-empty middle row) contribute an empty string, which is what keeps the
 * copied block's line count matching what was highlighted.
 */
export function selectedFrameRowTexts(range: FrameSelectionRange, frame: FrameRow[]): string[] {
  const out: string[] = []
  for (let row = range.start.row; row <= range.end.row; row += 1) {
    const target = frame[row]
    if (!target) continue
    const fromCol = row === range.start.row ? range.start.col : target.leftGutter
    const toCol = row === range.end.row ? range.end.col : rowEndColumn(target)
    const span = relativeSpan(target, fromCol, toCol)
    if (!span) continue
    out.push(sliceByDisplayColumn(target.text, span.from, span.to))
  }
  return out
}

/** Convenience wrapper: the copy payload (LF-joined, no trailing newline). */
export function selectedFrameText(range: FrameSelectionRange, frame: FrameRow[]): string {
  return selectedFrameRowTexts(range, frame).join('\n')
}

/**
 * Per-row highlight spans, aligned with `frame` so a renderer can do `spans[row]`.
 *
 * Spans are relative to each row's own text (that is what the renderer paints), while
 * the range's columns are absolute canvas columns -- the translation happens here once
 * instead of in every view.
 */
export function frameSelectionSpans(
  range: FrameSelectionRange | null,
  frame: FrameRow[],
): Array<SelectionSpan | undefined> {
  const spans: Array<SelectionSpan | undefined> = new Array(frame.length).fill(undefined)
  if (!range) return spans
  for (let row = range.start.row; row <= range.end.row; row += 1) {
    const target = frame[row]
    if (!target) continue
    const fromCol = row === range.start.row ? range.start.col : target.leftGutter
    const toCol = row === range.end.row ? range.end.col : rowEndColumn(target)
    const span = relativeSpan(target, fromCol, toCol)
    // A click without a drag paints nothing: a one-cell block looks like a glitch.
    if (!span || span.to <= span.from) continue
    spans[row] = span
  }
  return spans
}

/** Whether a click without a drag produced anything worth copying. */
export function isEmptyFrameSelection(range: FrameSelectionRange | null): boolean {
  if (!range) return true
  return range.start.row === range.end.row && range.start.col === range.end.col
}

/** One rendered region, described well enough to become frame rows. */
export type FrameSection = {
  /** Rendered text lines, top to bottom. */
  lines?: string[]
  /** Blank rows to reserve (gaps the layout budgets for but paints nothing). */
  blankRows?: number
  leftGutter: number
  columns: number
}

/**
 * Flatten the rendered sections into the frame row model, in render order.
 *
 * Callers pass sections in exactly the order they are painted, so a row index is a
 * screen row and a drag needs no knowledge of where the transcript ends and the
 * composer begins.
 */
export function buildFrameRows(sections: FrameSection[], blankColumns = 1): FrameRow[] {
  const rows: FrameRow[] = []
  for (const section of sections) {
    const gutter = Math.max(0, Math.floor(section.leftGutter))
    const columns = Math.max(1, Math.floor(section.columns))
    for (const line of section.lines ?? []) {
      rows.push({ text: line, leftGutter: gutter, columns })
    }
    const blanks = Math.max(0, Math.floor(section.blankRows ?? 0))
    for (let index = 0; index < blanks; index += 1) {
      rows.push({ text: '', leftGutter: gutter, columns })
    }
  }
  void blankColumns
  return rows
}

/**
 * Force the frame to exactly `count` rows, which is what the layout budget says the
 * canvas is. The bottom band is a fixed-height, `overflow="hidden"` box: too few rows
 * would leave the composer floating above the bottom edge, and too many would be
 * clipped -- silently shifting every row below the clip out of the selection model.
 */
export function fitFrameRows(rows: FrameRow[], count: number, columns: number): FrameRow[] {
  const target = Math.max(0, Math.floor(count))
  if (rows.length === target) return rows
  if (rows.length > target) return rows.slice(0, target)
  const width = Math.max(1, Math.floor(columns))
  const filler: FrameRow = { text: '', leftGutter: 0, columns: width }
  return [...rows, ...new Array(target - rows.length).fill(filler)]
}

/** Composer rows as frame rows: the '> ' gutter is painted, so it is part of the text. */
export function composerFrameRows(
  lines: Array<{ gutter: string; text: string }>,
  canvasColumns: number,
): FrameRow[] {
  const columns = inputGutterColumns + inputContentColumns(canvasColumns)
  return lines.map(line => ({
    text: `${line.gutter}${line.text}`,
    leftGutter: inputLeftPaddingColumns,
    columns,
  }))
}

function displayWidth(text: string): number {
  return stringWidth(text)
}
