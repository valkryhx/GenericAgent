// Real GA Ink UI e2e for the /team (alias /agents) execution-time team panel.
// See docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md.
//
// Renders the REAL App with a fake TTY, starts the REAL bridge, types `/team`,
// and asserts the overlay panel actually painted (header + task board + footer)
// with no rendered line reaching the terminal width. No LLM turn is required, so
// this is fast and deterministic; it validates the *UI layout*, not a model answer.
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { render } from 'ink'
import stringWidth from 'string-width'
import { App } from '../src/App.js'
import { startBridge, type BridgeClient } from '../src/bridgeClient.js'
import { createCursorParkStdout } from '../src/stdoutCursorPark.js'
import { applyBridgeEvent, initialState, type AppState } from '../src/state.js'
import type { BridgeCommand, BridgeEvent } from '../src/protocol.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..')
const PYTHON = process.env.PYTHON || 'python'
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink', 'bridge.py')
const COLUMNS = 80

class CaptureWriteStream extends EventEmitter {
  columns = COLUMNS
  rows = 24
  isTTY = true
  chunks: string[] = []
  write(chunk: unknown): boolean {
    this.chunks.push(String(chunk))
    return true
  }
}

class FakeReadStream extends EventEmitter {
  isTTY = true
  private readonly queue: string[] = []
  setRawMode(): this { return this }
  setEncoding(): this { return this }
  ref(): this { return this }
  unref(): this { return this }
  resume(): this { return this }
  pause(): this { return this }
  read(): string | null { return this.queue.shift() ?? null }
  send(text: string): void { this.queue.push(text); this.emit('readable') }
}

const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
    .replace(/\r/g, '')
}

async function waitFor(label: string, predicate: () => boolean, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(100)
  }
  throw new Error(`${label} timed out`)
}

async function typeUserInput(stdin: FakeReadStream, text: string): Promise<void> {
  for (const character of text) {
    stdin.send(character)
    await delay(8)
  }
  stdin.send('\r')
}

async function main(): Promise<number> {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const cursorPark = createCursorParkStdout(stdout as unknown as NodeJS.WriteStream)
  const events: BridgeEvent[] = []
  let state: AppState = initialState
  let client: BridgeClient | null = null

  const startBridgeClient = (
    python: string,
    bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
    onExit: (code: number | null) => void,
  ): BridgeClient => {
    client = startBridge(python, bridgeScript, event => {
      events.push(event)
      state = applyBridgeEvent(state, event)
      onEvent(event)
    }, onExit)
    return client
  }

  const instance = render(React.createElement(App, {
    python: PYTHON,
    bridgeScript: BRIDGE_SCRIPT,
    startBridgeClient,
    cursorPark,
  }), {
    stdout: cursorPark.stdout,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
    debug: false,
  })

  let exitCode = 0
  try {
    await waitFor('bridge ready', () => events.some(event => event.type === 'ready'), 60_000)
    await waitFor('initial frame', () => stdout.chunks.length > 0, 10_000)

    await typeUserInput(stdin, '/team')
    await waitFor('team_list command', () => events.some(event => event.type === 'team_roster'), 30_000)
    await delay(600)

    // Ink writes diffs; pick the last chrome frame (same heuristic as the other real-UI e2e scripts).
    const chrome = stdout.chunks
      .map(stripAnsi)
      .filter(chunk => chunk.includes('Enter send') || chunk.includes('GenericAgent'))
    const tail = chrome.at(-1) || ''
    const frame = tail
    if (process.env.GA_TEAM_E2E_DEBUG === '1') {
      console.log('chrome frames:', chrome.length)
      console.log('--- last frame ---')
      console.log(tail)
      console.log('--- end ---')
    }

    assert.match(tail, /Team .*lead team-lead/, 'team panel header missing')
    assert.match(tail, /Task board/, 'team panel task board section missing')
    assert.match(tail, /Esc close/, 'team panel footer hint missing')
    assert.ok(
      frame.split('\n').every(line => stringWidth(line) < COLUMNS),
      'a rendered line reaches/exceeds the terminal width',
    )

    console.log('PASS /team panel rendered (header + task board + footer, width OK)')
    console.log(tail.split('\n').filter(line => /Team |Task board|Esc close|\[.\] #/.test(line)).join('\n'))
  } catch (error) {
    exitCode = 1
    console.error('FAIL', error instanceof Error ? error.message : String(error))
  } finally {
    instance.unmount()
    client?.stop()
  }
  return exitCode
}

main().then(code => process.exit(code))
