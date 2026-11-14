/**
 * Two things the user could not see, both reported from a real session.
 *
 * 1. **A queued prompt is invisible.** Typing mid-turn and pressing Enter cleared the
 *    composer and changed the hint to "Queued 1 message", but the text itself vanished
 *    from the screen. The user had no way to confirm *what* was queued -- only that
 *    something was. The agent's eventual reply proves it worked, but "it worked" is not
 *    the same as "I can see what I sent". A queued prompt should appear in the transcript
 *    exactly like a normal prompt, because that is what the user typed.
 *
 * 2. **A finished child reported as killed.** `probe_mid` completed its work (output
 *    file has MID_ONE / MID_TWO / MID_DONE, `completion_status: completed`) and was still
 *    announced in red as failed, because `close_agent` escalated to `terminate_process()`
 *    when the child had not exited within grace_s, and the status projection checks
 *    `process_status == "killed"` *before* `turn_status == "completed"`. How a process was
 *    reclaimed is not a statement about whether its work succeeded.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import React from 'react'
import { render } from 'ink'
import { App } from './App.js'
import { initialState } from './state.js'
import { inkRenderOptions } from './inkRenderOptions.js'
import type { BridgeClient } from './bridgeClient.js'
import type { BridgeEvent } from './protocol.js'

class CaptureWriteStream extends EventEmitter {
  columns = 100
  rows = 30
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
  read(): string | null { return this.queue.shift() ?? null }
  resume(): this { return this }
  pause(): this { return this }
  send(text: string): void {
    this.queue.push(text)
    this.emit('readable')
  }
}

function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, '')
    .replace(/\u001B\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\u001B[=>]/g, '')
}

/**
 * Everything painted after the composer was cleared, i.e. the transcript region.
 *
 * Matching the whole buffer is useless here: the text was visible in the composer while
 * it was being typed, so "does this string appear anywhere" passes without the queued
 * prompt ever reaching the transcript. The marker truncates the buffer at the last
 * `user` echo the App writes when the draft is queued.
 */
function transcriptAfterQueue(stdout: CaptureWriteStream, marker: string): string {
  const plain = stripAnsi(stdout.chunks.join(''))
  const at = plain.lastIndexOf(marker)
  return at < 0 ? '' : plain.slice(at)
}

function delay(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms))
}

async function waitUntil(predicate: () => boolean, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await delay(10)
  }
}

type Harness = {
  stdout: CaptureWriteStream
  stdin: FakeReadStream
  instance: ReturnType<typeof render>
  emit: (event: BridgeEvent) => void
  sent: string[]
}

function mountApp(): Harness {
  const stdout = new CaptureWriteStream()
  const stderr = new CaptureWriteStream()
  const stdin = new FakeReadStream()
  const sent: string[] = []
  let onEventRef: ((event: BridgeEvent) => void) | null = null

  const startBridgeClient = (
    _python: string,
    _bridgeScript: string,
    onEvent: (event: BridgeEvent) => void,
  ): BridgeClient => {
    onEventRef = onEvent
    setTimeout(() => onEvent({ type: 'ready', version: 1 }), 0)
    return {
      send(command: unknown) {
        sent.push(JSON.stringify(command))
      },
      stop() {},
    }
  }

  const instance = render(React.createElement(App, {
    python: 'python',
    bridgeScript: 'bridge.py',
    startBridgeClient,
  }), {
    ...inkRenderOptions,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    patchConsole: false,
  })

  return { stdout, stdin, instance, sent, emit: (event) => onEventRef?.(event) }
}

async function queueAPrompt(app: Harness, text: string): Promise<void> {
  app.emit({ type: 'status', status: 'running', taskId: 1 })
  await waitUntil(() => stripAnsi(app.stdout.chunks.join('')).includes('Running ·'))
  app.stdin.send(text)
  await delay(40)
  app.stdin.send('\r')
  await delay(120)
}

test('a queued prompt enters the transcript as a message', async () => {
  // Assert on the reducer, not on painted bytes: the composer already contains the text
  // while it is being typed, so any "does this string appear in the stream" check passes
  // without the prompt ever reaching the transcript. `messages` is what the transcript
  // renders from, so a message here *is* the user-visible line.
  const { applyBridgeEvent, initialState } = await import('./state.js')
  const queued = applyBridgeEvent(
    applyBridgeEvent(initialState, { type: 'status', status: 'running', taskId: 1 } as BridgeEvent),
    { type: 'prompt_queued', text: 'please also check the logs', queued: 1 } as BridgeEvent,
  )

  const hit = queued.messages.find(message => message.text.includes('please also check the logs'))
  assert.ok(hit, 'the queued prompt never became a transcript message')
  assert.equal(hit.role, 'user', 'a queued prompt should read as something the user said')
  assert.match(hit.text, /queued|Queued|排队/i, 'the line does not say it is waiting its turn')
})

test('a queued prompt is announced once, not per re-render', async () => {
  const { applyBridgeEvent, initialState } = await import('./state.js')
  let state = applyBridgeEvent(initialState, { type: 'status', status: 'running', taskId: 1 } as BridgeEvent)
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'only once please', queued: 1 } as BridgeEvent)
  // The bridge reports the queue depth again on the next acceptance; the same text must
  // not paint a second line.
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'only once please', queued: 1 } as BridgeEvent)

  const occurrences = state.messages.filter(message => message.text.includes('only once please')).length
  assert.equal(occurrences, 1, `the queued prompt was added ${occurrences} times`)
})

test('a queued prompt that becomes a real turn is not shown twice', async () => {
  // Every queued prompt eventually arrives as a `user` event too: Esc interrupts and
  // re-asks with it, and the bridge promotes it to a turn when the run ends first. The
  // placeholder line and the real turn are the same message, so keeping both would make
  // the user's single sentence appear twice in the transcript.
  const { applyBridgeEvent, initialState } = await import('./state.js')
  let state = applyBridgeEvent(initialState, { type: 'status', status: 'running', taskId: 1 } as BridgeEvent)
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'answer this instead', queued: 1 } as BridgeEvent)
  state = applyBridgeEvent(state, { type: 'user', taskId: 2, text: 'answer this instead' } as BridgeEvent)

  const occurrences = state.messages.filter(message => message.text.includes('answer this instead')).length
  assert.equal(occurrences, 1, `the same message appeared ${occurrences} times`)
  assert.equal(state.messages.at(-1)?.role, 'user')
  assert.equal(state.messages.at(-1)?.taskId, 2, 'the surviving line is not the real turn')
})

test('a real turn does not delete an unrelated queued placeholder', async () => {
  const { applyBridgeEvent, initialState } = await import('./state.js')
  let state = applyBridgeEvent(initialState, { type: 'status', status: 'running', taskId: 1 } as BridgeEvent)
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'second thought, do X', queued: 1 } as BridgeEvent)
  state = applyBridgeEvent(state, { type: 'user', taskId: 2, text: 'completely different question' } as BridgeEvent)

  const pending = state.messages.filter(message => message.queuedPrompt)
  assert.equal(pending.length, 1, 'a still-queued message was removed by an unrelated turn')
})

test('a child that completed its work is announced as completed even if the process was killed', async () => {
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)
    // The exact shape probe_mid had: work done, process reclaimed by escalation.
    app.emit({
      type: 'agent_snapshot',
      snapshot: {
        records: [{
          executionId: 'process:probe_mid',
          engine: 'process',
          recordKind: 'process_agent',
          // What the bridge projects when process_status == "killed" wins the branch.
          status: 'killed',
          sourceStatus: 'completed',
          capabilities: { actions: [], features: [] },
          agentPath: '/root/probe_mid',
          metadata: { taskName: 'probe_mid', completionStatus: 'completed' },
        }] as never,
        cursors: {},
        errors: {},
      },
    })
    await delay(150)

    const frame = stripAnsi(app.stdout.chunks.join(''))
    assert.match(frame, /probe_mid/)
    assert.match(
      frame,
      /completed|完成/i,
      'a child that finished its work was announced as a failure because its process was reclaimed',
    )
    assert.doesNotMatch(
      frame.split('\n').filter(line => line.includes('probe_mid')).join('\n'),
      /failed|失败/i,
      'the announcement calls a successful child a failure',
    )
  } finally {
    app.instance.unmount()
  }
})

test('a child whose work genuinely failed is still announced as failed', async () => {
  // The guard against "fix it by calling everything a success".
  const app = mountApp()
  try {
    await waitUntil(() => app.stdout.chunks.length > 0)
    app.emit({
      type: 'agent_snapshot',
      snapshot: {
        records: [{
          executionId: 'process:probe_bad',
          engine: 'process',
          recordKind: 'process_agent',
          status: 'failed',
          sourceStatus: 'errored',
          capabilities: { actions: [], features: [] },
          agentPath: '/root/probe_bad',
          metadata: { taskName: 'probe_bad' },
        }] as never,
        cursors: {},
        errors: {},
      },
    })
    await delay(150)

    const frame = stripAnsi(app.stdout.chunks.join(''))
    assert.match(frame, /probe_bad/)
    assert.match(frame, /failed|失败/i, 'a genuinely failed child was not reported as failed')
  } finally {
    app.instance.unmount()
  }
})
