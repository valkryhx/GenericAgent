/**
 * Measures JSONL event receipt → App dispatch → in-memory stdout frame.
 * Root cause: src/App.tsx:1804 batches assistant deltas with an 80ms timer.
 * Determinism: send ready before the turn and wait for visible output before
 * sending done, so done cannot mask a stalled stream. Timings are sampled.
 * Keep this to distinguish scheduler delay from Ink rendering delay.
 * Run: npx tsx probes/stream_latency_timeline.ts [legacy|frame] [rounds]
 * On the original checkout, legacy measures stdout only. Frame mode requires
 * the App scheduler and onStreamDispatch observer from the candidate.
 */
import assert from 'node:assert/strict'
import { PassThrough } from 'node:stream'
import { stripVTControlCharacters } from 'node:util'
import { render } from 'ink'
import React from 'react'
import { App } from '../src/App.js'
import type { BridgeEvent } from '../src/protocol.js'

const delay = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms))
const timeout = 15000

async function waitFor(predicate: () => boolean, what: string) {
  const deadline = performance.now() + timeout
  while (!predicate()) {
    if (performance.now() >= deadline) throw new Error(`timed out after ${timeout}ms waiting for: ${what}`)
    await delay(2)
  }
}

async function runRound(round: number) {
  const stdout = Object.assign(new PassThrough(), { columns: 100, rows: 40 })
  const stdin = new PassThrough()
  const frames: { at: number; text: string }[] = []
  stdout.on('data', (chunk: Buffer) => frames.push({ at: performance.now(), text: stripVTControlCharacters(chunk.toString()) }))
  let send: (event: BridgeEvent) => void = () => { throw new Error('bridge is not ready') }
  let ready = false
  const dispatches: number[] = []
  const props: React.ComponentProps<typeof App> & { onStreamDispatch: () => void } = {
    python: 'unused', bridgeScript: 'unused',
    onStreamDispatch: () => dispatches.push(performance.now()),
    startBridgeClient: (_python, _script, onEvent) => {
      send = onEvent
      queueMicrotask(() => {
        onEvent({ type: 'ready', version: 1 })
        ready = true
      })
      return { send() {}, stop() {} }
    },
  }
  const app = render(React.createElement(App, props), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stderr: stdout as unknown as NodeJS.WriteStream,
    debug: true, patchConsole: false,
  })
  try {
    await waitFor(() => ready, 'bridge ready')
    send({ type: 'user', taskId: 1, text: 'Latency probe' })
    send({ type: 'status', status: 'running', taskId: 1 })
    const marker = `LATENCY${round}FIXTURE`
    const received = performance.now()
    const initialFrameCount = frames.length
    send({ type: 'assistant_delta', taskId: 1, text: '界' })
    send({ type: 'assistant_delta', taskId: 1, text: marker })
    await waitFor(() => frames.slice(initialFrameCount).some(frame => frame.text.includes(`界${marker}`)), 'visible assistant text before done')
    const first = frames.slice(initialFrameCount).find(frame => frame.text.includes('界'))!
    const dispatched = dispatches[0]
    assert.ok(received <= first.at, 'first frame preceded event receipt')
    if (dispatched !== undefined) {
      assert.ok(received <= dispatched && dispatched <= first.at, 'event, dispatch and frame out of order')
    } else {
      assert.equal(process.env.GA_STREAM_SCHEDULER, 'legacy', 'frame mode requires the App dispatch observer')
    }
    const streamWrites = frames.length - initialFrameCount
    const beforeDone = frames.length
    const tail = `${marker}FINAL`
    send({ type: 'assistant_done', taskId: 1, text: `界${marker} ${tail}` })
    send({ type: 'status', status: 'idle', taskId: 1 })
    await waitFor(() => frames.slice(beforeDone).some(frame => frame.text.includes(tail)), 'final frame with reconciled text')
    const final = frames.slice(beforeDone).find(frame => frame.text.includes(tail))!
    assert.equal(final.text.split(`界${marker} ${tail}`).length, 2, 'final text missing or duplicated')
    return { dispatch: dispatched === undefined ? undefined : dispatched - received, frame: first.at - received, streamWrites }
  } finally {
    app.unmount()
  }
}

async function main() {
  const mode = process.argv[2] ?? 'legacy'
  const rounds = Number(process.argv[3] ?? 5)
  assert.ok(mode === 'legacy' || mode === 'frame', 'expected legacy or frame')
  assert.ok(Number.isInteger(rounds) && rounds > 0, 'rounds must be a positive integer')
  process.env.GA_STREAM_SCHEDULER = mode
  const results = []
  for (let i = 0; i < rounds; i++) results.push(await runRound(i))
  const percentile = (values: number[], quantile: number) => values.sort((a, b) => a - b)[Math.ceil(quantile * rounds) - 1]!.toFixed(2)
  for (const stage of ['dispatch', 'frame'] as const) {
    const values = results.map(row => row[stage]).filter((value): value is number => value !== undefined)
    if (values.length === 0) {
      console.log(`${mode} event-to-${stage}: requires the App dispatch observer`)
      continue
    }
    assert.equal(values.length, rounds, `incomplete ${stage} samples`)
    console.log(`${mode} event-to-${stage} ms p50=${percentile(values, .5)} p90=${percentile(values, .9)}`)
  }
  console.log(`${mode} stdout writes before done: ${results.map(r => r.streamWrites).join(', ')}`)
  console.log(`✔ ${mode}: verified event order and final output in ${rounds}/${rounds} rounds (Ink debug mode)`)
}

main().catch(error => {
  console.error('✖', error)
  process.exitCode = 1
})
