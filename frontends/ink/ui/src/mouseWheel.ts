export type MouseWheelDirection = 'up' | 'down'
/**
 * How much mouse traffic GA asks the terminal to hand over.
 *
 * Why GA has to take the pointer at all
 * -------------------------------------
 * In the alternate screen most terminals enable DECSET 1007 ("alternate
 * scroll"): a wheel notch is translated by the *terminal* into `CSI A` / `CSI B`,
 * i.e. an ordinary up/down arrow key. GA receives `key.upArrow` and, having no
 * way to tell it apart from a real arrow key, routes it to composer history —
 * which is exactly the "wheel scrolls my input history, not the transcript"
 * symptom. There is no protocol-level way to distinguish the two without mouse
 * reporting, so the wheel can only reach the transcript if GA claims the mouse.
 *
 * That makes selection and wheel mutually exclusive *unless* the app implements
 * its own selection — which is what Claude Code does. CC 319 ships `full`
 * (`m1003_0x126e602e.js`: `mouseTracking === void 0 ? "full"`), takes the left
 * button, and draws its own selection highlight, copying through the clipboard
 * (m0701 `onSelectionStart` / `onSelectionDrag` / `onSelectionChange`). GA now
 * does the same, so taking the pointer no longer means losing copy-out.
 *
 * Modes
 * -----
 * - `off`    — report nothing. The terminal keeps native click-drag selection
 *   and native wheel scrollback, but GA cannot scroll its own transcript with
 *   the wheel (1007 turns notches into arrow keys → composer history).
 * - `scroll` — wheel only (`1000h` + `1006h`). In-app wheel paging, but no
 *   press/drag/release, so GA cannot draw its own selection either.
 * - `full`   — wheel + press/drag/release (`1000h` + `1002h` + `1006h`). This is
 *   what in-app selection needs, and therefore the default.
 *
 * `1003h` (bare any-motion, no button held) is deliberately NOT requested even
 * in `full`: moving the pointer over the window would emit a report per cell and
 * force an Ink re-render each time. `1002h` already covers "motion while a button
 * is held", which is all selecting needs.
 */
export type MouseCaptureMode = 'off' | 'scroll' | 'full'

export type MouseEvent =
  | { kind: 'wheel'; direction: MouseWheelDirection; x: number; y: number }
  | { kind: 'press' | 'drag' | 'release'; button: number; x: number; y: number }

const fullMouseTrackingOn = '\u001B[?1000h\u001B[?1002h\u001B[?1006h'
const scrollMouseTrackingOn = '\u001B[?1000h\u001B[?1006h'

export function resolveMouseCaptureMode(env: Record<string, string | undefined> = process.env): MouseCaptureMode {
  const value = env.GA_INK_MOUSE?.trim().toLowerCase()
  if (value === 'full' || value === '1' || value === 'true' || value === 'on') return 'full'
  if (value === 'scroll' || value === 'wheel') return 'scroll'
  if (value === 'off' || value === '0' || value === 'false' || value === 'none') return 'off'

  const legacyValue = env.GA_ENABLE_MOUSE_DRAG?.trim().toLowerCase()
  if (legacyValue === 'full' || legacyValue === '1' || legacyValue === 'true' || legacyValue === 'on') return 'full'
  if (legacyValue === 'off' || legacyValue === '0' || legacyValue === 'false' || legacyValue === 'none') return 'off'

  // Default: full capture, matching Claude Code 319.
  //
  // `off` looks tempting because it keeps the terminal's own click-drag
  // selection, but it also means the wheel can never reach GA: in the alternate
  // screen the terminal translates wheel notches into up/down arrow keys
  // (DECSET 1007), which GA must then treat as composer-history navigation. The
  // transcript becomes unreachable by mouse. Taking the pointer is the only way
  // to get wheel scrolling, and once GA has the pointer it must draw its own
  // selection — otherwise nothing is selectable at all.
  //
  // GA_INK_MOUSE=off    → native selection, wheel scrolls input history
  // GA_INK_MOUSE=scroll → wheel pages the transcript, no selection
  // GA_INK_MOUSE=full   → wheel pages the transcript + in-app selection
  return 'full'
}

export function mouseTrackingOn(mode: MouseCaptureMode = resolveMouseCaptureMode()): string {
  if (mode === 'full') return fullMouseTrackingOn
  if (mode === 'scroll') return scrollMouseTrackingOn
  return ''
}

export function mouseTrackingOff(): string {
  return '\u001B[?1006l\u001B[?1003l\u001B[?1002l\u001B[?1000l'
}

/**
 * DECSET 1007 "alternate scroll": while in the alternate screen, the terminal
 * turns wheel notches into `CSI A` / `CSI B`. With tracking on, the terminal
 * sends real mouse reports instead and never consults 1007 — but leaving it on is
 * still wrong, because the moment a terminal (or a multiplexer) decides to be
 * clever it will inject arrow keys that GA must then treat as composer-history
 * navigation, exactly the bug this change fixes. Turn it off explicitly so the
 * wheel has one meaning and only one meaning.
 *
 * Harmless where unsupported: unknown DECSET numbers are ignored.
 */
export function alternateScrollOff(): string {
  return '\u001B[?1007l'
}

export function alternateScrollOn(): string {
  return '\u001B[?1007h'
}

/**
 * Whether to use the alternate screen + pinned root height.
 *
 * This is deliberately independent of `MouseCaptureMode`. Fullscreen layout is
 * what holds the composer still; mouse capture is what steals text selection.
 * Coupling them forced a choice between "composer moves" and "cannot copy", when
 * the two are unrelated.
 *
 * `GA_INK_MOUSE=off` used to mean "inline mode"; it now only turns mouse capture
 * off. Use `GA_INK_FULLSCREEN=off` for the old inline layout.
 */
export function resolveFullscreenMode(env: Record<string, string | undefined> = process.env): boolean {
  const value = env.GA_INK_FULLSCREEN?.trim().toLowerCase()
  if (value === 'off' || value === '0' || value === 'false' || value === 'none') return false
  if (value === 'on' || value === '1' || value === 'true') return true

  // Claude Code 2.1.319 defaults to the alternate screen (`N9e()` returns true).
  // Inline mode cannot hold the composer fixed: history is written above the live
  // frame through ink <Static>, so each new line pushes it down, and the terminal
  // scrolls once the screen fills.
  return true
}

export function parseMouseWheel(input: string): MouseWheelDirection | null {
  const event = parseMouseEvent(input)
  if (event?.kind === 'wheel') return event.direction
  return null
}

export function parseMouseEvent(input: string): MouseEvent | null {
  const sgrMatch = /\u001B?\[<(\d+);\d+;\d+[Mm]/.exec(input)
  if (sgrMatch) {
    const fullMatch = /\u001B?\[<(\d+);(\d+);(\d+)([Mm])/.exec(input)
    if (!fullMatch) return null
    const button = Number(fullMatch[1])
    const x = Number(fullMatch[2])
    const y = Number(fullMatch[3])
    const wheel = wheelDirectionFromButton(button)
    if (wheel) return { kind: 'wheel', direction: wheel, x, y }
    if (fullMatch[4] === 'm') return { kind: 'release', button: button & 0x03, x, y }
    if ((button & 0x20) !== 0) return { kind: 'drag', button: button & 0x03, x, y }
    return { kind: 'press', button: button & 0x03, x, y }
  }

  const x10Match = /\u001B?\[M([\s\S])[\s\S][\s\S]/.exec(input)
  if (x10Match) {
    const fullMatch = /\u001B?\[M([\s\S])([\s\S])([\s\S])/.exec(input)
    if (!fullMatch) return null
    const button = fullMatch[1].charCodeAt(0) - 32
    const x = fullMatch[2].charCodeAt(0) - 32
    const y = fullMatch[3].charCodeAt(0) - 32
    const wheel = wheelDirectionFromButton(button)
    if (wheel) return { kind: 'wheel', direction: wheel, x, y }
    if ((button & 0x20) !== 0) return { kind: 'drag', button: button & 0x03, x, y }
    return { kind: 'press', button: button & 0x03, x, y }
  }

  return null
}

function wheelDirectionFromButton(button: number): MouseWheelDirection | null {
  if ((button & 0x43) === 0x40) {
    return 'up'
  }
  if ((button & 0x43) === 0x41) {
    return 'down'
  }
  return null
}
