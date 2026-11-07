/**
 * In-app text selection for the transcript viewport.
 *
 * Why this exists: taking the mouse (needed so the wheel can page the transcript
 * instead of being turned into arrow keys by DECSET 1007) costs the terminal's
 * own click-drag selection. Claude Code pays the same price and pays it the same
 * way — it takes the pointer and draws its own highlight, copying through the
 * clipboard (319 `m0701`: `onSelectionStart` / `onSelectionDrag` /
 * `onSelectionChange`). This module is the pure, testable half of that: turning
 * two viewport coordinates into a highlight span and into the text to copy.
 *
 * Coordinates are *viewport* coordinates, not screen coordinates:
 *   row 0 = first visible transcript row, col 0 = first display column of the
 *   message content area (the area already excludes the 1-column gutter and the
 *   canvas reserve). The caller translates mouse y/x into these.
 */

import stringWidth from 'string-width'

export type SelectionPoint = {
  /** Zero-based index into the visible transcript rows. */
  row: number
  /** Zero-based display column within the content area. */
  col: number
}

export type SelectionRange = {
  start: SelectionPoint
  end: SelectionPoint
}

/** A highlight span on one row: columns are half-open `[from, to)`. */
export type SelectionSpan = {
  /** Zero-based display column where the highlight begins. */
  from: number
  /** Zero-based display column where the highlight ends (exclusive). */
  to: number
}

export type SelectionInput = {
  rows: string[]
  /** Display width of the content area, used to clamp columns at line end. */
  contentColumns: number
}

function clampPoint(point: SelectionPoint, rows: string[], contentColumns: number): SelectionPoint {
  const rowCount = rows.length
  const row = rowCount === 0 ? 0 : Math.min(rowCount - 1, Math.max(0, Math.floor(point.row)))
  const columns = Math.max(1, Math.floor(contentColumns))
  const col = Math.min(columns, Math.max(0, Math.floor(point.col)))
  return { row, col }
}

/**
 * Order two points so `start` is always before `end`, regardless of drag
 * direction. Dragging up-left is as normal as down-right.
 */
export function normalizeSelection(
  anchor: SelectionPoint,
  focus: SelectionPoint,
  rows: string[],
  contentColumns: number,
): SelectionRange {
  const a = clampPoint(anchor, rows, contentColumns)
  const b = clampPoint(focus, rows, contentColumns)
  const aFirst = a.row < b.row || (a.row === b.row && a.col <= b.col)
  return aFirst ? { start: a, end: b } : { start: b, end: a }
}

/**
 * The selected text, one entry per row.
 *
 * A row's visible end is its text width, not `contentColumns`: rows are already
 * wrapped, so trailing padding is not part of the transcript. The final row of a
 * selection is also cut at the cursor column, which is what makes a mid-line
 * drag copy only up to the cursor.
 */
export function selectedRowTexts(range: SelectionRange, rows: string[], contentColumns: number): string[] {
  const out: string[] = []
  for (let row = range.start.row; row <= range.end.row; row += 1) {
    const text = rows[row] ?? ''
    const width = Math.min(displayWidth(text), Math.max(1, Math.floor(contentColumns)))
    const from = row === range.start.row ? Math.min(range.start.col, width) : 0
    const to = row === range.end.row ? Math.min(range.end.col, width) : width
    if (to < from) continue
    out.push(sliceByDisplayColumn(text, from, to))
  }
  return out
}

/** Convenience wrapper: the copy payload (LF-joined, no trailing newline). */
export function selectedText(range: SelectionRange, rows: string[], contentColumns: number): string {
  return selectedRowTexts(range, rows, contentColumns).join('\n')
}

/**
 * Per-row highlight spans for the rows the selection touches.
 *
 * Returns a sparse array aligned with the visible rows so the renderer can do
 * `spans[index]` without a lookup. Empty rows outside the selection are
 * `undefined`, and rows whose span is empty (a single click with no drag) are
 * omitted too — a one-cell selection should not paint a half-character block.
 */
export function selectionSpans(
  range: SelectionRange | null,
  rows: string[],
  contentColumns: number,
): Array<SelectionSpan | undefined> {
  const spans: Array<SelectionSpan | undefined> = new Array(rows.length).fill(undefined)
  if (!range) return spans
  const columns = Math.max(1, Math.floor(contentColumns))
  for (let row = range.start.row; row <= range.end.row; row += 1) {
    const width = Math.min(displayWidth(rows[row] ?? ''), columns)
    const from = row === range.start.row ? Math.min(range.start.col, width) : 0
    const to = row === range.end.row ? Math.min(range.end.col, width) : width
    if (to <= from) continue
    spans[row] = { from, to }
  }
  return spans
}

/** Whether a click without a drag produced anything worth copying. */
export function isEmptySelection(range: SelectionRange | null): boolean {
  if (!range) return true
  return range.start.row === range.end.row && range.start.col === range.end.col
}

// ---------------------------------------------------------------------------
// Display-width helpers
//
// These must agree with how Ink lays the row out, which is `string-width`
// (East-Asian wide = 2 columns, combining marks = 0). Slicing by JS string index
// would mis-cut every CJK row, so both functions walk code points.
// ---------------------------------------------------------------------------

function displayWidth(text: string): number {
  return stringWidth(text)
}

/**
 * Slice `[from, to)` display columns out of `text`, keeping whole code points.
 * A column that lands inside a wide character includes that character (it would
 * otherwise render as a broken half-cell).
 *
 * NOTE: this is a *single* slice. Do NOT call it three times with adjacent ranges
 * to build a highlighted row — a wide character straddling both boundaries is
 * returned by every slice that overlaps it, so the pieces duplicate it and the
 * row grows. Use `partitionByDisplayColumns` for that.
 */
export function sliceByDisplayColumn(text: string, from: number, to: number): string {
  let column = 0
  let out = ''
  for (const char of text) {
    const charWidth = stringWidth(char)
    const charStart = column
    const charEnd = column + charWidth
    column = charEnd
    if (charEnd <= from) continue
    if (charStart >= to) break
    out += char
  }
  return out
}

/**
 * Split `text` into the three pieces a highlighted row needs, in ONE pass, so
 * every character lands in exactly one bucket.
 *
 * This exists because the naive version (three `sliceByDisplayColumn` calls) is
 * not a partition: `sliceByDisplayColumn` includes any character that *overlaps*
 * the range, so a CJK character straddling a boundary is emitted by both
 * neighbours. `回答内容` highlighted at columns 3..5 produced
 * `before=回答`, `selected=答内`, `after=内容` — 8 characters out of 4. The row
 * got wider every time the cursor crossed a wide character, and with
 * `wrap="truncate-end"` the truncation point slid left and right: the user-visible
 * "the text jitters horizontally while I drag" bug.
 *
 * Character assignment:
 *   - ends at or before `from`  → before
 *   - starts at or after `to`    → after
 *   - otherwise                  → selected
 *
 * Concatenating the three results always reproduces `text` exactly, so the row
 * width never changes and nothing shifts.
 */
export function partitionByDisplayColumns(
  text: string,
  from: number,
  to: number,
): { before: string; selected: string; after: string } {
  let column = 0
  let before = ''
  let selected = ''
  let after = ''
  for (const char of text) {
    const charWidth = stringWidth(char)
    const charStart = column
    const charEnd = column + charWidth
    column = charEnd
    if (charEnd <= from) before += char
    else if (charStart >= to) after += char
    else selected += char
  }
  return { before, selected, after }
}

/** Column at which `text` ends in display cells (used to clamp row ends). */
export function displayColumnEnd(text: string): number {
  return stringWidth(text)
}
