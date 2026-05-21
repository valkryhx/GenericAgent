export const exitTerminalCleanupSequence = '\u001B[0m\u001B[?25h\r\u001B[2K'

export function cleanupTerminalForExit(stdout: Pick<NodeJS.WriteStream, 'write'>): void {
  stdout.write(exitTerminalCleanupSequence)
}
