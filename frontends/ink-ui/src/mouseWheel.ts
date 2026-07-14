export type MouseWheelDirection = 'up' | 'down'
export type MouseCaptureMode = 'off' | 'full'

export type MouseEvent =
  | { kind: 'wheel'; direction: MouseWheelDirection; x: number; y: number }
  | { kind: 'press' | 'drag' | 'release'; button: number; x: number; y: number }

const fullMouseTrackingOn = '\u001B[?1000h\u001B[?1002h\u001B[?1003h\u001B[?1006h'

export function resolveMouseCaptureMode(env: Record<string, string | undefined> = process.env): MouseCaptureMode {
  const value = env.GA_INK_MOUSE?.trim().toLowerCase()
  if (value === 'full' || value === '1' || value === 'true' || value === 'on') return 'full'
  if (value === 'off' || value === '0' || value === 'false' || value === 'none') return 'off'

  const legacyValue = env.GA_ENABLE_MOUSE_DRAG?.trim().toLowerCase()
  if (legacyValue === 'full' || legacyValue === '1' || legacyValue === 'true' || legacyValue === 'on') return 'full'
  return 'off'
}

export function mouseTrackingOn(mode: MouseCaptureMode = resolveMouseCaptureMode()): string {
  return mode === 'full' ? fullMouseTrackingOn : ''
}

export function mouseTrackingOff(): string {
  return '\u001B[?1006l\u001B[?1003l\u001B[?1002l\u001B[?1000l'
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
