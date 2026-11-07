import test from 'node:test'
import assert from 'node:assert/strict'
import { planMessageViewport } from './messageViewportPlan.js'

test('planMessageViewport keeps one steady row when live is empty (no full-height spacer)', () => {
  // Codex content-desired: idle + Static → no tall empty live slot between history and composer.
  // One row, not zero: `live/1` -> `none/0` at turn end shrinks the whole frame by a row,
  // which on a full terminal reads as the picture jumping up one line at every turn end.
  assert.deepEqual(
    planMessageViewport({ hasStaticMessages: true, liveLineCount: 0, messageRows: 18 }),
    { kind: 'live', height: 1 },
  )
})

test('planMessageViewport keeps frame height steady across the turn boundary', () => {
  // The composer row must not move when a turn ends: `keepLivePlaceholder` is on while
  // running and off once idle. Both states must plan the same viewport height.
  const heightOf = (plan: ReturnType<typeof planMessageViewport>) => (plan.kind === 'none' ? 0 : plan.height)
  const runningWithHistory = planMessageViewport({
    hasStaticMessages: true,
    liveLineCount: 0,
    messageRows: 18,
    keepLivePlaceholder: true,
  })
  const idleWithHistory = planMessageViewport({
    hasStaticMessages: true,
    liveLineCount: 0,
    messageRows: 18,
  })
  assert.equal(heightOf(idleWithHistory), heightOf(runningWithHistory))
})

test('planMessageViewport shows a compact ready row before any transcript exists', () => {
  assert.deepEqual(
    planMessageViewport({ hasStaticMessages: false, liveLineCount: 0, messageRows: 18 }),
    { kind: 'ready', height: 1 },
  )
})

test('planMessageViewport sizes live viewport to streaming content (capped), not full messageRows', () => {
  assert.deepEqual(
    planMessageViewport({ hasStaticMessages: true, liveLineCount: 2, messageRows: 18 }),
    { kind: 'live', height: 2 },
  )
})

test('planMessageViewport grows with stream until maxLiveRows then caps (触底固定 live 高)', () => {
  const short = planMessageViewport({
    hasStaticMessages: true, liveLineCount: 3, messageRows: 18, maxLiveRows: 12,
  })
  const mid = planMessageViewport({
    hasStaticMessages: true, liveLineCount: 12, messageRows: 18, maxLiveRows: 12,
  })
  const over = planMessageViewport({
    hasStaticMessages: true, liveLineCount: 40, messageRows: 18, maxLiveRows: 12,
  })
  assert.deepEqual(short, { kind: 'live', height: 3 })
  assert.deepEqual(mid, { kind: 'live', height: 12 })
  // Also never exceed remaining messageRows (terminal room).
  assert.deepEqual(over, { kind: 'live', height: 12 })
})

test('planMessageViewport never exceeds messageRows when room is tight', () => {
  assert.deepEqual(
    planMessageViewport({ hasStaticMessages: true, liveLineCount: 20, messageRows: 5, maxLiveRows: 12 }),
    { kind: 'live', height: 5 },
  )
})

test('planMessageViewport keeps a 1-row live placeholder while running with no stream lines yet', () => {
  assert.deepEqual(
    planMessageViewport({
      hasStaticMessages: true,
      liveLineCount: 0,
      messageRows: 18,
      keepLivePlaceholder: true,
    }),
    { kind: 'live', height: 1 },
  )
})
