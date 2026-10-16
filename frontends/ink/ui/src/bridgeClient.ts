import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { createInterface } from 'node:readline'
import type { BridgeCommand, BridgeEvent } from './protocol.js'

export type BridgeClient = {
  send: (command: BridgeCommand) => void
  stop: () => void
}

export function buildBridgeEnv(baseEnv: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  return {
    ...baseEnv,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
  }
}

type BridgeStdin = {
  write(chunk: string): unknown
  on?(event: 'error', listener: (error: unknown) => void): unknown
  destroyed?: boolean
  writableEnded?: boolean
}

export function writeBridgeCommand(stdin: BridgeStdin, command: BridgeCommand): void {
  if (stdin.destroyed || stdin.writableEnded) return
  try {
    stdin.write(`${JSON.stringify(command)}
`)
  } catch {
    // The bridge may already be gone during Ctrl+C or app teardown.
  }
}

export function guardBridgeStdin(stdin: BridgeStdin): void {
  // A closed child pipe reports EPIPE asynchronously via the stream's 'error'
  // event, which a synchronous try/catch cannot intercept. Without a listener
  // that event is unhandled and tears down the whole Ink host during teardown.
  stdin.on?.('error', () => {})
}

export function startBridge(
  python: string,
  bridgeScript: string,
  onEvent: (event: BridgeEvent) => void,
  onExit: (code: number | null) => void,
): BridgeClient {
  const child: ChildProcessWithoutNullStreams = spawn(python, [bridgeScript], {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: buildBridgeEnv(),
  })
  guardBridgeStdin(child.stdin)
  const stdout = createInterface({ input: child.stdout })
  stdout.on('line', line => {
    try {
      onEvent(JSON.parse(line) as BridgeEvent)
    } catch (error) {
      onEvent({ type: 'error', code: 'bad_bridge_event', message: String(error) })
    }
  })
  child.stderr.on('data', chunk => {
    onEvent({ type: 'error', code: 'bridge_stderr', message: String(chunk) })
  })
  child.on('exit', code => onExit(code))

  let stopped = false
  return {
    send(command: BridgeCommand) {
      writeBridgeCommand(child.stdin, command)
    },
    stop() {
      if (stopped) return
      stopped = true
      writeBridgeCommand(child.stdin, { type: 'shutdown' })
      if (child.exitCode === null && child.signalCode === null) child.kill()
    },
  }
}
