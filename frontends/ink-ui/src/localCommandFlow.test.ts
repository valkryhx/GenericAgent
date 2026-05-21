import test from 'node:test'
import assert from 'node:assert/strict'
import { pendingLocalCommandAfterBridgeEvent } from './localCommandFlow.js'

test('bridge local command output resolves pending compact command before history replacement', () => {
  let pending: string | null = '/compact'

  pending = pendingLocalCommandAfterBridgeEvent(pending, {
    type: 'local_command_output',
    text: 'Compacted 8 messages into summary context.',
  })

  assert.equal(pending, null)
})
