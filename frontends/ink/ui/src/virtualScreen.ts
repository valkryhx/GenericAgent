/**
 * A minimal terminal screen, so tests can read back what was actually painted.
 *
 * Ink writes a stream of cursor moves, erases and text, not a picture. Splitting that
 * stream on "looks like a frame boundary" works until it doesn't (multi-line frames,
 * partial repaints, cursor moves inside a line), and a wrong split makes a selection test
 * assert against a frame that was never on screen.
 *
 * Claude Code does not have this problem because it keeps its own
 * `frontFrame.screen` grid and reads selection text out of it. This is the same idea,
 * small enough to audit: apply the handful of sequences Ink actually emits and keep a
 * rows x columns grid of characters.
 */

const CSI = '\u001B['

export type Screen = {
  rows: string[]
  row: number
  col: number
}

export function createScreen(rows = 30, columns = 100): Screen {
  return {
    rows: new Array(rows).fill('').map(() => ' '.repeat(columns)),
    row: 0,
    col: 0,
  }
}

function ensureRow(screen: Screen, row: number): void {
  while (screen.rows.length <= row) screen.rows.push(' '.repeat(screen.rows[0]?.length ?? 80))
}

function put(screen: Screen, text: string, columns: number): void {
  ensureRow(screen, screen.row)
  const current = screen.rows[screen.row]!
  const padded = current.length < columns ? current + ' '.repeat(columns - current.length) : current
  const cells = padded.split('')
  let col = Math.min(screen.col, columns)
  for (const char of text) {
    if (col >= columns) break
    cells[col] = char
    col += 1
  }
  screen.rows[screen.row] = cells.join('')
  screen.col = col
}

/**
 * Apply a chunk of terminal output to the screen.
 *
 * Deliberately narrow: only the sequences Ink emits in its normal (non-autowrap) mode,
 * plus erase-line and erase-display. Anything else is skipped rather than guessed at,
 * so an unknown sequence cannot silently corrupt the grid.
 */
export function applyToScreen(screen: Screen, chunk: string, columns = 100): void {
  let index = 0
  while (index < chunk.length) {
    if (chunk.startsWith(CSI, index)) {
      const match = /^\u001B\[([0-9;?]*)([A-Za-z])/.exec(chunk.slice(index))
      if (!match) { index += 1; continue }
      const params = match[1] ?? ''
      const command = match[2]!
      index += match[0].length
      const first = Number(params.split(';')[0] || '0')
      if (command === 'A') screen.row = Math.max(0, screen.row - Math.max(1, first))
      else if (command === 'B') screen.row += Math.max(1, first)
      else if (command === 'C') screen.col = Math.min(columns, screen.col + Math.max(1, first))
      else if (command === 'D') screen.col = Math.max(0, screen.col - Math.max(1, first))
      else if (command === 'G') screen.col = Math.max(0, first - 1)
      else if (command === 'H' || command === 'f') {
        const parts = params.split(';')
        screen.row = Math.max(0, Number(parts[0] || '1') - 1)
        screen.col = Math.max(0, Number(parts[1] || '1') - 1)
      } else if (command === 'J') {
        if (first === 2) {
          for (let row = 0; row < screen.rows.length; row += 1) {
            screen.rows[row] = ' '.repeat(columns)
          }
          screen.row = 0
          screen.col = 0
        }
      } else if (command === 'K') {
        ensureRow(screen, screen.row)
        const current = screen.rows[screen.row]!
        if (first === 0) {
          screen.rows[screen.row] = current.slice(0, screen.col) + ' '.repeat(Math.max(0, columns - screen.col))
        } else if (first === 2) {
          screen.rows[screen.row] = ' '.repeat(columns)
        }
      }
      // Everything else (?25l, ?2026h, SGR m, ...) has no effect on the character grid.
      continue
    }
    if (chunk.startsWith('\u001B]', index)) {
      // OSC: skip to BEL or ST.
      const bel = chunk.indexOf('\u0007', index)
      const st = chunk.indexOf('\u001B\\', index)
      const end = bel === -1 ? st : st === -1 ? bel : Math.min(bel, st)
      index = end === -1 ? chunk.length : end + (end === bel ? 1 : 2)
      continue
    }
    if (chunk[index] === '\u001B') { index += 1; continue }
    const char = chunk[index]!
    index += 1
    if (char === '\r') { screen.col = 0; continue }
    if (char === '\n') { screen.row += 1; screen.col = 0; ensureRow(screen, screen.row); continue }
    if (char === '\t') { screen.col = Math.min(columns, (Math.floor(screen.col / 8) + 1) * 8); continue }
    if (char < ' ') continue
    put(screen, char, columns)
  }
}

/** Read back the painted lines, trailing spaces trimmed. */
export function screenLines(screen: Screen): string[] {
  return screen.rows.map(line => line.replace(/\s+$/, ''))
}

/**
 * Replay a raw terminal-output stream and return the visible screen.
 *
 * This only works against a stream that positions the cursor absolutely, which is what
 * Ink does in its normal rendering mode. In `debug: true` Ink instead prints every frame
 * sequentially with no addressing, and there is no screen to reconstruct -- a caller that
 * needs this must render with `debug: false`.
 *
 * The whole stream is replayed rather than the segment after the last erase-display: Ink
 * repaints by moving the cursor and overwriting, so earlier output is legitimately
 * overwritten in place and dropping it would lose whatever the last partial repaint did
 * not touch.
 *
 * Because the grid can grow past the terminal height (a frame taller than the window), the
 * result is clipped to the final `rows` lines -- the window a user would actually see.
 */
export function paintedFrame(raw: string, rows: number, columns: number): string[] {
  const screen = createScreen(rows, columns)
  applyToScreen(screen, raw, columns)
  const lines = screenLines(screen)
  return lines.length > rows ? lines.slice(lines.length - rows) : lines
}

/** Row index of the last line matching `pattern` in an already-rendered frame.
 *
 *  Bottom-up on purpose: an earlier frame can leave stale text in rows the latest frame
 *  did not overwrite, and the composer is always in the lower part of the screen.
 */
export function frameRow(lines: string[], pattern: RegExp): number {
  for (let row = lines.length - 1; row >= 0; row -= 1) {
    if (pattern.test(lines[row]!)) return row
  }
  return -1
}
