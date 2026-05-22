const showCursor = '\u001B[?25h'

export const enterMainScreenTerminalSequence = ''
export const exitTerminalCleanupSequence = `\u001B[0m${showCursor}\r\u001B[2K`

export function cleanupTerminalForExit(stdout: Pick<NodeJS.WriteStream, 'write'>): void {
  stdout.write(exitTerminalCleanupSequence)
}
