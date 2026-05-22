import test from 'node:test'
import assert from 'node:assert/strict'
import { planMessageViewport } from './messageViewportPlan.js'

test('planMessageViewport does not reserve an empty transcript viewport when history is in terminal scrollback', () => {
  assert.deepEqual(planMessageViewport({ hasStaticMessages: true, liveLineCount: 0, messageRows: 18 }), { kind: 'none' })
})

test('planMessageViewport shows a compact ready row before any transcript exists', () => {
  assert.deepEqual(planMessageViewport({ hasStaticMessages: false, liveLineCount: 0, messageRows: 18 }), { kind: 'ready' })
})

test('planMessageViewport reserves a fixed live viewport while assistant output is streaming', () => {
  assert.deepEqual(planMessageViewport({ hasStaticMessages: true, liveLineCount: 2, messageRows: 18 }), { kind: 'live', height: 18 })
})
