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

export function writeBridgeCommand(
  stdin: Pick<NodeJS.WritableStream, 'write'>,
  command: BridgeCommand,
): void {
  try {
    stdin.write(`${JSON.stringify(command)}\n`)
  } catch {
    // The bridge may already be gone during Ctrl+C or app teardown.
  }
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

  return {
    send(command: BridgeCommand) {
      writeBridgeCommand(child.stdin, command)
    },
    stop() {
      writeBridgeCommand(child.stdin, { type: 'shutdown' })
      child.kill()
    },
  }
}
