/**
 * Click targets for thinking blocks in the transcript.
 *
 * The collapsed reasoning row renders `▸ ✻ <summary>` and the expanded one
 * `▾ ✻ thinking`. A triangle is a disclosure affordance: terminals draw it, users
 * click it, and nothing happened — the mouse handler only ever did text selection
 * and the "jump to bottom" pill. This module is the hit test that makes the
 * triangle (and the rest of the block) toggle like Ctrl+O does.
 *
 * Kept pure and separate from `App.tsx` because the App-level version is only
 * reachable through a real terminal with mouse reporting enabled.
 */

/** A visible transcript row that belongs to a thinking block. */
export type ThinkingRow = {
  /** Row index inside the transcript viewport. */
  row: number
  /** `header` is the `▸ ✻ …` line; `body` rows are the expanded text. */
  kind: 'header' | 'body'
}

/** Row id shape produced by `messageWindow.appendThinkingRows`. */
const THINKING_ROW_ID = /-thinking-\d+(?:-line-\d+)?$/

/**
 * The disclosure header: an optional indent, the collapsed/expanded triangle, the
 * star, then the summary or the literal `thinking` label.
 *
 * Both glyph pairs come from `GLYPHS`, so a theme or font swap that changes them
 * must change this together with the renderer.
 */
const THINKING_HEADER = /^\s*[▸▾]\s*✻/

/**
 * Map visible transcript rows to thinking-block rows.
 *
 * Only rows actually on screen are considered: the caller scrolls the transcript,
 * so a row number that is not in this list is not something the user clicked.
 */
export function thinkingRowsOf(lines: { id: string; text: string }[]): ThinkingRow[] {
  const rows: ThinkingRow[] = []
  lines.forEach((line, row) => {
    if (!THINKING_ROW_ID.test(line.id)) return
    rows.push({ row, kind: THINKING_HEADER.test(line.text) ? 'header' : 'body' })
  })
  return rows
}

/**
 * True when a click at `row` should toggle the thinking disclosure.
 *
 * The whole block is a target, not just the triangle: the summary line spans the
 * width, and requiring a precise hit on a 2-column glyph would read as flaky
 * rather than as a control. Clicks outside every thinking block fall through to
 * ordinary text selection.
 */
export function hitTestThinkingRow(rows: ThinkingRow[], row: number): boolean {
  return rows.some(entry => entry.row === row)
}
