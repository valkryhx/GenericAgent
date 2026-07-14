/**
 * Codex-aligned inline viewport geometry for history insert + dock placement.
 *
 * Mirrors the control flow in codex-rs/tui `insert_history.rs` / `tui.draw`:
 * - viewport is a bottom dock band: { areaY, areaHeight }
 * - inserting history while not bottom-aligned: areaY += min(lines, roomBelow)
 * - once bottom-aligned, areaY freezes; further history only scrolls above
 * - dock height growth re-sticks to bottom when overflowing the screen
 *
 * This module is the pure geometry + ANSI sequence builder. App can feed
 * Static commit row counts and live dock heights into it. Full Ink stdout
 * takeover is optional; tests lock the Codex math regardless of wiring depth.
 */

export type ViewportState = {
  screenRows: number
  screenCols: number
  /** Top row of the dock (0-based). */
  areaY: number
  /** Dock height in rows (composer + live tail + chrome). */
  areaHeight: number
  dockHeight: number
}

export type ViewportInit = {
  screenRows: number
  screenCols: number
  dockHeight: number
  /** Optional starting y; default places dock near top content (y=0). */
  areaY?: number
}

export function createViewportState(input: ViewportInit): ViewportState {
  const screenRows = Math.max(1, Math.floor(input.screenRows))
  const screenCols = Math.max(1, Math.floor(input.screenCols))
  const dockHeight = Math.max(1, Math.min(screenRows, Math.floor(input.dockHeight)))
  const maxY = Math.max(0, screenRows - dockHeight)
  const areaY = input.areaY === undefined
    ? 0
    : Math.max(0, Math.min(maxY, Math.floor(input.areaY)))
  return {
    screenRows,
    screenCols,
    areaY,
    areaHeight: dockHeight,
    dockHeight,
  }
}

export function isBottomAligned(state: ViewportState): boolean {
  return state.areaY + state.areaHeight >= state.screenRows
}

/**
 * Codex insert_history: if viewport bottom < screen height, move dock down.
 * Returns new state and how many rows the dock shifted (scroll_amount).
 */
export function advanceViewportForHistory(
  state: ViewportState,
  historyLines: number,
): { state: ViewportState; scrollAmount: number } {
  const lines = Math.max(0, Math.floor(historyLines))
  if (lines === 0) return { state, scrollAmount: 0 }

  const bottom = state.areaY + state.areaHeight
  if (bottom >= state.screenRows) {
    return { state, scrollAmount: 0 }
  }

  const roomBelow = state.screenRows - bottom
  const scrollAmount = Math.min(lines, roomBelow)
  return {
    state: {
      ...state,
      areaY: state.areaY + scrollAmount,
    },
    scrollAmount,
  }
}

/**
 * Codex tui.draw: when dock desired height changes, clamp and re-stick to bottom
 * if the band would overflow the screen.
 */
export function applyDockHeight(state: ViewportState, dockHeight: number): ViewportState {
  const height = Math.max(1, Math.min(state.screenRows, Math.floor(dockHeight)))
  let areaY = state.areaY
  if (areaY + height > state.screenRows) {
    areaY = Math.max(0, state.screenRows - height)
  }
  return {
    ...state,
    areaHeight: height,
    dockHeight: height,
    areaY,
  }
}

export type InsertHistoryResult = {
  state: ViewportState
  scrollAmount: number
  /** ANSI sequence approximating Codex scroll-region insert (for optional raw write). */
  sequence: string
}

/**
 * Build a best-effort ANSI sequence for inserting `historyLines` above the dock.
 * Geometry always updates via advanceViewportForHistory.
 *
 * IMPORTANT: Do not write this sequence while Ink owns stdout. Concurrent DECSTBM/RI
 * races Ink's frame redraw and causes stacked composers / duplicated headers
 * (regression evidence: 截图/新bug.png). Keep for unit tests and a future coordinated
 * renderer that fully owns the terminal (true Codex insert_history path).
 */
export function insertHistorySequence(
  state: ViewportState,
  historyLines: number,
): InsertHistoryResult {
  const lines = Math.max(0, Math.floor(historyLines))
  const { state: advanced, scrollAmount } = advanceViewportForHistory(state, lines)

  if (lines === 0) {
    return { state: advanced, scrollAmount: 0, sequence: '' }
  }

  // Save cursor, set scroll region to rows above dock, insert newlines, restore.
  // CSI rows are 1-based. Dock top = advanced.areaY (0-based) → region end = areaY.
  const dockTop1 = Math.max(1, advanced.areaY) // 1-based exclusive end for region above dock
  const parts: string[] = []
  parts.push('\x1b[s') // save cursor

  if (scrollAmount > 0) {
    // Reverse-index style push: scroll the band from old dock top to screen bottom down.
    const oldTop1 = Math.max(1, state.areaY + 1)
    parts.push(`\x1b[${oldTop1};${state.screenRows}r`)
    parts.push(`\x1b[${oldTop1};1H`)
    for (let i = 0; i < scrollAmount; i++) {
      parts.push('\x1bM') // reverse index (scroll down / move viewport content down)
    }
    parts.push('\x1b[r') // reset scroll region
  }

  // Insert history lines into the region above the (new) dock top.
  if (dockTop1 > 1) {
    parts.push(`\x1b[1;${dockTop1}r`)
    parts.push(`\x1b[${dockTop1};1H`)
    for (let i = 0; i < lines; i++) {
      parts.push('\r\n')
    }
    parts.push('\x1b[r')
  } else if (!scrollAmount) {
    // Already at top with no room: just emit newlines into normal scrollback.
    for (let i = 0; i < lines; i++) {
      parts.push('\r\n')
    }
  }

  parts.push('\x1b[u') // restore cursor
  return {
    state: advanced,
    scrollAmount,
    sequence: parts.join(''),
  }
}
