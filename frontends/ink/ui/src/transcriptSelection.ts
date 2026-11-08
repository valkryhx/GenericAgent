/**
 * Display-column primitives shared by every text surface.
 *
 * History: this module used to own the *whole* selection model, in transcript-viewport
 * coordinates. That model could not express "the drag started in the composer", so it was
 * replaced by `frameSelection` / `framePlan`, which work in absolute frame coordinates
 * across every region. What is left here is the part that was never viewport-specific:
 * the display-width arithmetic, kept in one place so transcript rows, composer rows and
 * highlights all cut text the same way. Slicing by JS string index would mis-cut every
 * CJK row.
 */

import stringWidth from 'string-width'

/** A highlight span on one row: columns are half-open `[from, to)`. */
export type SelectionSpan = {
  /** Zero-based display column where the highlight begins. */
  from: number
  /** Zero-based display column where the highlight ends (exclusive). */
  to: number
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
