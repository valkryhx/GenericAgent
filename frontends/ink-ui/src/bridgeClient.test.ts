import test from 'node:test'
import assert from 'node:assert/strict'
import { buildBridgeEnv, writeBridgeCommand } from './bridgeClient.js'

test('buildBridgeEnv forces Python stdio to UTF-8', () => {
  const env = buildBridgeEnv({ PATH: 'x' })

  assert.equal(env.PYTHONIOENCODING, 'utf-8')
  assert.equal(env.PYTHONUTF8, '1')
  assert.equal(env.PATH, 'x')
})

test('writeBridgeCommand ignores closed pipe errors', () => {
  const stdin = {
    write() {
      throw new Error('closed')
    },
  }

  assert.doesNotThrow(() => writeBridgeCommand(stdin, { type: 'stop' }))
})
