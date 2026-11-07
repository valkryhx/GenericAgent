import test from 'node:test'
import assert from 'node:assert/strict'
import { mouseTrackingOff, mouseTrackingOn, parseMouseEvent, parseMouseWheel, resolveFullscreenMode, resolveMouseCaptureMode } from './mouseWheel.js'

test('parseMouseWheel recognizes SGR wheel reports with or without leading escape', () => {
  assert.equal(parseMouseWheel('\u001B[<64;12;4M'), 'up')
  assert.equal(parseMouseWheel('[<65;12;4M'), 'down')
})

test('parseMouseWheel recognizes X10 wheel reports', () => {
  assert.equal(parseMouseWheel('\u001B[M`!!'), 'up')
  assert.equal(parseMouseWheel('\u001B[Ma!!'), 'down')
})

test('parseMouseWheel ignores non-wheel input', () => {
  assert.equal(parseMouseWheel('a'), null)
  assert.equal(parseMouseWheel('\u001B[A'), null)
  assert.equal(parseMouseWheel('\u001B[<0;12;4M'), null)
})

test('mouse capture defaults to full so the wheel can page the transcript', () => {
  // `off` cannot coexist with wheel scrolling: in the alternate screen the
  // terminal translates wheel notches into up/down arrow keys (DECSET 1007), so
  // GA sees `key.upArrow` and pages composer history instead of the transcript.
  // Capturing the mouse is the only way to receive real wheel reports — and once
  // the pointer is captured GA must draw its own selection (transcriptSelection.ts).
  assert.equal(resolveMouseCaptureMode({}), 'full')
  assert.equal(resolveMouseCaptureMode({ GA_INK_MOUSE: 'scroll' }), 'scroll')
  assert.equal(resolveMouseCaptureMode({ GA_INK_MOUSE: 'full' }), 'full')
  assert.equal(resolveMouseCaptureMode({ GA_INK_MOUSE: 'off' }), 'off')
  assert.equal(resolveMouseCaptureMode({ GA_INK_MOUSE: 'off', GA_ENABLE_MOUSE_DRAG: 'true' }), 'off')
  assert.equal(resolveMouseCaptureMode({ GA_ENABLE_MOUSE_DRAG: 'true' }), 'full')
  assert.equal(resolveMouseCaptureMode({ GA_ENABLE_MOUSE_DRAG: 'off' }), 'off')
  assert.equal(mouseTrackingOn('off'), '')
  // scroll reports the wheel only: no 1002h (button) and no 1003h (any motion).
  assert.equal(mouseTrackingOn('scroll'), '\u001B[?1000h\u001B[?1006h')
  assert.doesNotMatch(mouseTrackingOn('scroll'), /\?1003h/)
  // full adds button press/drag so the app can draw its own selection, but still
  // never asks for 1003h: bare motion without a held button would report every
  // cell the pointer crosses and force a re-render each time.
  assert.equal(mouseTrackingOn('full'), '\u001B[?1000h\u001B[?1002h\u001B[?1006h')
  assert.doesNotMatch(mouseTrackingOn('full'), /\?1003h/)
  assert.equal(mouseTrackingOff(), '\u001B[?1006l\u001B[?1003l\u001B[?1002l\u001B[?1000l')
})

test('fullscreen mode is on by default and independent of mouse capture', () => {
  // Alternate screen pins the root height, which is what stops the composer from
  // sliding down. It must not be tied to mouse capture.
  assert.equal(resolveFullscreenMode({}), true)
  assert.equal(resolveFullscreenMode({ GA_INK_FULLSCREEN: 'off' }), false)
  assert.equal(resolveFullscreenMode({ GA_INK_FULLSCREEN: 'on' }), true)
  assert.equal(resolveMouseCaptureMode({}), 'full')
  assert.equal(resolveFullscreenMode({ GA_INK_MOUSE: 'off' }), true)
})

test('parseMouseEvent recognizes SGR press drag and release events', () => {
  assert.deepEqual(parseMouseEvent('\u001B[<0;80;5M'), { kind: 'press', button: 0, x: 80, y: 5 })
  assert.deepEqual(parseMouseEvent('\u001B[<32;80;6M'), { kind: 'drag', button: 0, x: 80, y: 6 })
  assert.deepEqual(parseMouseEvent('\u001B[<0;80;6m'), { kind: 'release', button: 0, x: 80, y: 6 })
})

test('parseMouseEvent keeps wheel events distinct from button drags', () => {
  assert.deepEqual(parseMouseEvent('\u001B[<64;80;5M'), { kind: 'wheel', direction: 'up', x: 80, y: 5 })
  assert.deepEqual(parseMouseEvent('[<65;80;5M'), { kind: 'wheel', direction: 'down', x: 80, y: 5 })
})
