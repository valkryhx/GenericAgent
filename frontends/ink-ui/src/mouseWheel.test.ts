import test from 'node:test'
import assert from 'node:assert/strict'
import { mouseTrackingOff, mouseTrackingOn, parseMouseEvent, parseMouseWheel } from './mouseWheel.js'

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

test('mouse tracking sequences enable and disable SGR wheel reporting', () => {
  assert.equal(mouseTrackingOn(), '\u001B[?1000h\u001B[?1002h\u001B[?1003h\u001B[?1006h')
  assert.equal(mouseTrackingOff(), '\u001B[?1006l\u001B[?1003l\u001B[?1002l\u001B[?1000l')
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
