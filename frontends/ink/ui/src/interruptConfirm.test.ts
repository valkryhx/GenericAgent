import test from 'node:test'
import assert from 'node:assert/strict'
import {
  decideInterrupt,
  INTERRUPT_CONFIRM_WINDOW_MS,
  interruptConfirmHint,
} from './interruptConfirm.js'

test('decideInterrupt copies instead of interrupting when a selection exists', () => {
  // Claude Code 319 `m2124:8688`: with a selection, ctrl+c returns `!0` and the
  // event is swallowed, so the Global `app:interrupt` binding never runs.
  assert.deepEqual(
    decideInterrupt({ hasSelection: true, armedAt: null, now: 1000 }),
    { kind: 'copy-selection' },
  )
  // Even mid-confirmation: copying must not silently exit.
  assert.deepEqual(
    decideInterrupt({ hasSelection: true, armedAt: 900, now: 1000 }),
    { kind: 'copy-selection' },
  )
})

test('decideInterrupt arms on the first press and exits on the second', () => {
  const first = decideInterrupt({ hasSelection: false, armedAt: null, now: 1000 })
  assert.deepEqual(first, { kind: 'arm' })

  // Second press inside the window exits.
  const second = decideInterrupt({ hasSelection: false, armedAt: 1000, now: 1000 + INTERRUPT_CONFIRM_WINDOW_MS })
  assert.deepEqual(second, { kind: 'exit' })
})

test('decideInterrupt re-arms once the window lapses', () => {
  const stale = decideInterrupt({
    hasSelection: false,
    armedAt: 1000,
    now: 1000 + INTERRUPT_CONFIRM_WINDOW_MS + 1,
  })
  assert.deepEqual(stale, { kind: 'arm' })
})

test('decideInterrupt never exits without a prior arm', () => {
  for (const now of [0, 1, 10 ** 9]) {
    assert.deepEqual(decideInterrupt({ hasSelection: false, armedAt: null, now }), { kind: 'arm' })
  }
})

test('decideInterrupt accepts a custom window', () => {
  assert.deepEqual(
    decideInterrupt({ hasSelection: false, armedAt: 0, now: 500, windowMs: 400 }),
    { kind: 'arm' },
  )
  assert.deepEqual(
    decideInterrupt({ hasSelection: false, armedAt: 0, now: 300, windowMs: 400 }),
    { kind: 'exit' },
  )
})

test('interruptConfirmHint tells the user the second press exits', () => {
  assert.match(interruptConfirmHint(), /Ctrl\+C/)
  assert.match(interruptConfirmHint(), /again/i)
})
