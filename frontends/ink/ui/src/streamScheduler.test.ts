import assert from 'node:assert/strict'
import test from 'node:test'
import { createStreamScheduler } from './streamScheduler.js'

function fixture(leading = true) {
  let time = 0
  const timers = new Map<number, { due: number; run: () => void }>()
  let id = 0
  const output: [number, string][] = []
  const scheduler = createStreamScheduler({
    intervalMs: leading ? 24 : 80,
    leading,
    dispatch: (task, text) => output.push([task, text]),
    now: () => time,
    schedule: (run, wait) => { timers.set(++id, { due: time + wait, run }); return id },
    cancel: handle => { timers.delete(handle) },
  })
  const advance = (at: number) => {
    time = at
    for (const [key, timer] of [...timers]) {
      if (timer.due > at) continue
      timers.delete(key)
      timer.run()
    }
  }
  return { scheduler, output, advance, timers }
}

test('first short delta dispatches immediately and tail drains at the frame deadline even during silence', () => {
  const { scheduler, output, advance, timers } = fixture()
  scheduler.push(1, '')
  scheduler.push(1, '界')
  assert.deepEqual(output, [[1, '界']])
  advance(5)
  scheduler.push(1, '甲')
  advance(13)
  scheduler.push(1, '。')
  assert.equal(timers.size, 1)
  advance(23)
  assert.deepEqual(output, [[1, '界']])
  advance(24)
  assert.deepEqual(output, [[1, '界'], [1, '甲。']])
  advance(40)
  scheduler.push(1, '乙')
  assert.equal(output.length, 2)
  advance(48)
  assert.deepEqual(output.at(-1), [1, '乙'])
  advance(100)
  scheduler.push(1, '末')
  assert.deepEqual(output.at(-1), [1, '末'])
})

test('legacy waits one window and keeps pending text separate by task', () => {
  const { scheduler, output, advance, timers } = fixture(false)
  scheduler.push(20, '末')
  scheduler.push(3, '甲')
  scheduler.push(20, '尾')
  assert.equal(timers.size, 1)
  advance(79)
  assert.deepEqual(output, [])
  advance(80)
  assert.deepEqual(output, [[3, '甲'], [20, '末尾']])
})

test('boundary flush delivers the tail once and clear cancels stale work', () => {
  const { scheduler, output, advance, timers } = fixture()
  scheduler.push(2, 'first')
  scheduler.push(2, 'tail')
  scheduler.flush()
  scheduler.flush()
  assert.equal(timers.size, 0)
  assert.deepEqual(output, [[2, 'first'], [2, 'tail']])
  scheduler.push(2, 'stale')
  scheduler.clear()
  advance(999)
  assert.deepEqual(output, [[2, 'first'], [2, 'tail']])
})

test('a second task starts immediately and subsequent tails share one timer', () => {
  const { scheduler, output, advance, timers } = fixture()
  scheduler.push(5, 'a')
  scheduler.push(5, 'b')
  scheduler.push(3, 'c')
  scheduler.push(3, 'd')
  assert.deepEqual(output, [[5, 'a'], [3, 'c']])
  assert.equal(timers.size, 1)
  advance(24)
  assert.deepEqual(output, [[5, 'a'], [3, 'c'], [3, 'd'], [5, 'b']])
})
