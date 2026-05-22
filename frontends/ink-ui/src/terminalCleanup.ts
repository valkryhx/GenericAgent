import { mouseTrackingOff, mouseTrackingOn } from './mouseWheel.js'

const showCursor = '\u001B[?25h'
const enterAlternateScreen = '\u001B[?1049h\u001B[2J\u001B[H'
const exitAlternateScreen = '\u001B[?1049l'

export const enterMainScreenTerminalSequence = `${enterAlternateScreen}${mouseTrackingOn()}`
export const exitTerminalCleanupSequence = `${mouseTrackingOff()}\u001B[0m${showCursor}\r\u001B[2K${exitAlternateScreen}`

export function cleanupTerminalForExit(stdout: Pick<NodeJS.WriteStream, 'write'>): void {
  stdout.write(exitTerminalCleanupSequence)
}
