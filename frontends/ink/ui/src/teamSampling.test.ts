import test from 'node:test'
import assert from 'node:assert/strict'
import { advanceSampling, SAMPLING_STALL_LIMIT, type SamplingProgress } from './teamSampling.js'

const base: SamplingProgress = {
  completed: 5,
  inProgress: 10,
  pending: 0,
  liveWorkers: 3,
  leadIdle: true,
  total: 15,
}

test('a changing board resets the stall counter', () => {
  let state = { key: '', stalled: 0 }
  let result = advanceSampling(state, base)
  state = result.state
  assert.equal(result.stop, null)
  result = advanceSampling(state, { ...base, completed: 6, inProgress: 9 })
  assert.equal(result.state.stalled, 0)
  assert.equal(result.stop, null)
})

test('a live worker keeps the run going even when the board looks stuck', () => {
  // Run 3's bug: the lead was assigning tasks 6-15, every worker was alive, but the
  // completed count had not moved for four samples -- and the script stopped.
  let state = { key: '', stalled: 0 }
  for (let index = 0; index < SAMPLING_STALL_LIMIT * 5; index += 1) {
    const result = advanceSampling(state, base)
    state = result.state
    assert.equal(result.stop, null, `stopped with ${base.liveWorkers} live workers at sample ${index}`)
  }
})

test('an idle lead with no live worker stops after the limit', () => {
  let state = { key: '', stalled: 0 }
  let stop: string | null = null
  for (let index = 0; index <= SAMPLING_STALL_LIMIT; index += 1) {
    const result = advanceSampling(state, { ...base, liveWorkers: 0, inProgress: 0, pending: 10 })
    state = result.state
    stop = result.stop
  }
  assert.equal(stop, 'stalled')
})

test('a complete board stops immediately', () => {
  const result = advanceSampling({ key: '', stalled: 0 }, { ...base, completed: 15, inProgress: 0 })
  assert.equal(result.stop, 'done')
})

test('a busy lead never stops the loop', () => {
  const result = advanceSampling({ key: '', stalled: 99 }, { ...base, completed: 15, leadIdle: false })
  assert.equal(result.stop, null)
})
