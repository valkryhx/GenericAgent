/**
 * The virtual screen.
 *
 * It exists so a test can read back what was painted, which is the only way to check a
 * geometry mapping against reality instead of against a re-derivation of the same
 * formula. That makes it the one piece of test infrastructure whose own bugs would
 * silently make every geometry test lie, so it is tested directly.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { applyToScreen, createScreen, frameRow, paintedFrame, screenLines } from './virtualScreen.js'

test('plain text lands on the grid and trims trailing blanks', () => {
  const screen = createScreen(3, 20)
  applyToScreen(screen, 'hello', 20)
  assert.deepEqual(screenLines(screen), ['hello', '', ''])
})

test('cursor-move sequences reposition later text', () => {
  const screen = createScreen(4, 20)
  // Write "top", jump to row 3 col 1, write "bottom".
  applyToScreen(screen, `top\u001B[3;1Hbottom`, 20)
  assert.deepEqual(screenLines(screen).slice(0, 4), ['top', '', 'bottom', ''])
})

test('CUP home and a newline-terminated multi-line frame repaint in place', () => {
  const screen = createScreen(4, 20)
  applyToScreen(screen, `old\u001B[1;1Hnew\nline two`, 20)
  // The home sequence moved to (0,0); "new" overwrote "old", then a newline moved down.
  assert.equal(screenLines(screen)[0], 'new')
  assert.equal(screenLines(screen)[1], 'line two')
})

test('erase-line clears to the end of the row', () => {
  const screen = createScreen(2, 12)
  applyToScreen(screen, 'abcdefgh', 12)
  applyToScreen(screen, '\u001B[1;4H\u001B[K', 12)
  assert.equal(screenLines(screen)[0], 'abc')
})

test('erase-display resets the whole grid, which is how a repaint starts', () => {
  const screen = createScreen(3, 12)
  applyToScreen(screen, 'stale content\nmore stale', 12)
  applyToScreen(screen, '\u001B[2J\u001B[1;1Hfresh', 12)
  assert.deepEqual(screenLines(screen), ['fresh', '', ''])
})

test('SGR colour sequences do not consume cells', () => {
  const screen = createScreen(2, 20)
  applyToScreen(screen, '\u001B[32m\u001B[1mgreen\u001B[0m plain', 20)
  assert.equal(screenLines(screen)[0], 'green plain')
})

test('OSC sequences are skipped whole, including their terminator', () => {
  const screen = createScreen(2, 20)
  applyToScreen(screen, 'before\u001B]52;c;aGVsbG8=\u0007after', 20)
  assert.equal(screenLines(screen)[0], 'beforeafter')
})

test('frameRow searches bottom-up so the latest frame wins', () => {
  const screen = createScreen(6, 20)
  // Two frames' worth of the same marker: the lower one is the current frame.
  applyToScreen(screen, '> stale\nfiller\nfiller\n> current', 20)
  const lines = screenLines(screen)
  assert.equal(frameRow(lines, /^> /), 3)
  assert.equal(frameRow(lines, /nothing here/), -1)
})

test('a fullscreen-sized frame repaint reads back exactly what was written', () => {
  // Wide enough for the longest line, so the test is about the grid and not about
  // clipping (which the harness asserts elsewhere).
  const canvas = 60
  const screen = createScreen(10, canvas)
  const frame = [
    'GenericAgent                       running',
    'alpha one',
    'beta two',
    '',
    '✻ Working (3s)',
    ' > hello world',
    ' Enter send · Alt+Enter newline',
    '─'.repeat(canvas),
  ].join('\n')
  applyToScreen(screen, `${'\u001B[2J'}\u001B[1;1H${frame}`, canvas)
  const lines = screenLines(screen)
  assert.equal(lines[0], 'GenericAgent                       running')
  assert.equal(lines[5], ' > hello world')
  assert.equal(frameRow(lines, /^ > /), 5)
})

test('paintedFrame replays a cursor-addressed stream and clips to the terminal height', () => {
  // Ink in normal mode repaints by moving the cursor and overwriting; replaying the whole
  // stream is what reproduces the visible screen. A frame taller than the window is
  // clipped to its last `rows` lines, because that is the part still on screen.
  const chunk = `\u001B[2J\u001B[1;1Hheader\none\ntwo\n > typed`
  assert.deepEqual(paintedFrame(chunk, 6, 20).slice(0, 4), ['header', 'one', 'two', ' > typed'])

  const long = `\u001B[2J\u001B[1;1H${['a', 'b', 'c', 'd', 'e'].join('\n')}`
  assert.deepEqual(paintedFrame(long, 3, 20), ['c', 'd', 'e'])
})
