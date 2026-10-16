import test from 'node:test'
import assert from 'node:assert/strict'
import {
  APPROVAL_OPTIONS,
  approvalFromPermissionRequest,
  approvalPanelOnEnter,
  approvalPanelOnEscape,
  approvalPanelOnSettled,
  enqueueApprovalRequest,
  moveApprovalSelection,
  type ApprovalPanelState,
} from './approvalPanel.js'

const sampleEvent = {
  type: 'permission_request' as const,
  requestId: 'r1',
  toolName: 'file_write',
  argsPreview: '{"path":"a.txt"}',
  reason: 'approval_required:static_write_or_execute',
  mode: 'ask',
}

test('approvalFromPermissionRequest maps event fields', () => {
  const req = approvalFromPermissionRequest(sampleEvent)
  assert.equal(req.requestId, 'r1')
  assert.equal(req.toolName, 'file_write')
  assert.equal(req.argsPreview, '{"path":"a.txt"}')
})

test('enqueue creates panel then queues second request', () => {
  const a = approvalFromPermissionRequest(sampleEvent)
  const b = approvalFromPermissionRequest({ ...sampleEvent, requestId: 'r2', toolName: 'code_run' })
  const p1 = enqueueApprovalRequest(null, a)
  assert.equal(p1.current.requestId, 'r1')
  assert.equal(p1.queue.length, 0)
  const p2 = enqueueApprovalRequest(p1, b)
  assert.equal(p2.current.requestId, 'r1')
  assert.equal(p2.queue.length, 1)
  assert.equal(p2.queue[0].requestId, 'r2')
})

test('moveApprovalSelection clamps to accept/deny', () => {
  assert.equal(moveApprovalSelection(0, -1), 0)
  assert.equal(moveApprovalSelection(0, 1), 1)
  assert.equal(moveApprovalSelection(1, 1), 1)
})

test('Enter on accept responds accept and closes when queue empty', () => {
  const panel: ApprovalPanelState = {
    current: approvalFromPermissionRequest(sampleEvent),
    selected: 0,
    queue: [],
  }
  const action = approvalPanelOnEnter(panel)
  assert.equal(action.type, 'respond')
  if (action.type === 'respond') {
    assert.equal(action.decision, 'accept')
    assert.equal(action.requestId, 'r1')
    assert.equal(action.next, null)
  }
})

test('Enter on deny responds deny', () => {
  const panel: ApprovalPanelState = {
    current: approvalFromPermissionRequest(sampleEvent),
    selected: 1,
    queue: [],
  }
  const action = approvalPanelOnEnter(panel)
  assert.equal(action.type, 'respond')
  if (action.type === 'respond') {
    assert.equal(action.decision, 'deny')
  }
})

test('Esc always denies', () => {
  const panel: ApprovalPanelState = {
    current: approvalFromPermissionRequest(sampleEvent),
    selected: 0,
    queue: [],
  }
  const action = approvalPanelOnEscape(panel)
  assert.equal(action.type, 'respond')
  if (action.type === 'respond') {
    assert.equal(action.decision, 'deny')
  }
})

test('respond advances queue to next request', () => {
  const panel: ApprovalPanelState = {
    current: approvalFromPermissionRequest(sampleEvent),
    selected: 0,
    queue: [approvalFromPermissionRequest({ ...sampleEvent, requestId: 'r2', toolName: 'code_run' })],
  }
  const action = approvalPanelOnEnter(panel)
  assert.equal(action.type, 'respond')
  if (action.type === 'respond') {
    assert.equal(action.next?.current.requestId, 'r2')
    assert.equal(action.next?.current.toolName, 'code_run')
    assert.equal(action.next?.queue.length, 0)
  }
})

test('settled on current advances or closes', () => {
  const withQueue: ApprovalPanelState = {
    current: approvalFromPermissionRequest(sampleEvent),
    selected: 0,
    queue: [approvalFromPermissionRequest({ ...sampleEvent, requestId: 'r2' })],
  }
  const next = approvalPanelOnSettled(withQueue, 'r1')
  assert.equal(next?.current.requestId, 'r2')
  assert.equal(approvalPanelOnSettled(withQueue, 'r1')?.queue.length, 0)
  assert.equal(approvalPanelOnSettled({ ...withQueue, queue: [] }, 'r1'), null)
})

test('APPROVAL_OPTIONS is only accept and deny', () => {
  assert.deepEqual(
    APPROVAL_OPTIONS.map(o => o.decision),
    ['accept', 'deny'],
  )
})
