import test from 'node:test'
import assert from 'node:assert/strict'
import {
  nextStopEchoGate,
  pendingLocalCommandAfterBridgeEvent,
  shouldEchoStopTranscript,
  stopEchoGateAfterStatus,
} from './localCommandFlow.js'

test('bridge local command output resolves pending compact command (failure path)', () => {
  let pending: string | null = '/compact'

  pending = pendingLocalCommandAfterBridgeEvent(pending, {
    type: 'local_command_output',
    text: 'Compact failed: No conversation history to compact.',
  })

  assert.equal(pending, null)
})

test('compact success history_replace is not cleared by pendingLocalCommand helper (App clears)', () => {
  // 成功路径不再发 local_command_output；App 在 history_replace 分支清 pending。
  const pending = pendingLocalCommandAfterBridgeEvent('/compact', {
    type: 'history_replace',
    messages: [{ role: 'system', text: 'Compacted 8 messages into summary context.' }],
  })
  assert.equal(pending, '/compact')
})

test('shouldEchoStopTranscript only allows one echo per stop cycle', () => {
  assert.equal(shouldEchoStopTranscript(false), true)
  assert.equal(shouldEchoStopTranscript(true), false)
})

test('stopEchoGateAfterStatus resets only when a new task starts (running)', () => {
  // 关键：idle 时绝不能重置，否则 abort 回 idle 后双 Enter 会双显
  assert.equal(stopEchoGateAfterStatus('running'), 'reset')
  assert.equal(stopEchoGateAfterStatus('idle'), 'keep')
  assert.equal(stopEchoGateAfterStatus('stopping'), 'keep')
  assert.equal(stopEchoGateAfterStatus(undefined), 'keep')
  assert.equal(stopEchoGateAfterStatus(null), 'keep')
})

test('stop echo state machine: double stop before next run only echoes once', () => {
  let echoed = false

  // 第一次 /stop
  let step = nextStopEchoGate({ alreadyEchoed: echoed, isStopCommand: true })
  assert.equal(step.echo, true)
  echoed = step.nextEchoed

  // 模拟 stopping / idle 状态事件：闸门保持
  assert.equal(stopEchoGateAfterStatus('stopping'), 'keep')
  assert.equal(stopEchoGateAfterStatus('idle'), 'keep')

  // 第二次 /stop（双 Enter 或连按）
  step = nextStopEchoGate({ alreadyEchoed: echoed, isStopCommand: true })
  assert.equal(step.echo, false)
  echoed = step.nextEchoed

  // 新任务 running 后才允许再次回显
  if (stopEchoGateAfterStatus('running') === 'reset') echoed = false
  step = nextStopEchoGate({ alreadyEchoed: echoed, isStopCommand: true })
  assert.equal(step.echo, true)
})
