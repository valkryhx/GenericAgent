import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  enqueueGoalProposal,
  goalProposalFromEvent,
  goalProposalOnEnter,
  goalProposalOnEscape,
  goalProposalOnSettled,
  moveGoalProposalSelection,
} from './goalProposalPanel.js'

test('fromEvent extracts request fields', () => {
  const req = goalProposalFromEvent({ type: 'goal_proposal_request', requestId: 'r1', condition: 'all tests pass' })
  assert.equal(req.requestId, 'r1')
  assert.equal(req.condition, 'all tests pass')
})

test('enqueue creates then is idempotent', () => {
  const p1 = enqueueGoalProposal(null, { requestId: 'r1', condition: 'c' })
  assert.equal(p1.current.requestId, 'r1')
  assert.equal(p1.selected, 0)
  const p2 = enqueueGoalProposal(p1, { requestId: 'r1', condition: 'c' })
  assert.equal(p2, p1)
})

test('selection clamps', () => {
  assert.equal(moveGoalProposalSelection(0, -1), 0)
  assert.equal(moveGoalProposalSelection(0, 1), 1)
  assert.equal(moveGoalProposalSelection(1, 1), 1)
})

test('enter approve default / reject when second', () => {
  const p = { current: { requestId: 'r', condition: 'c' }, selected: 0 }
  assert.equal(goalProposalOnEnter(p).decision, 'approve')
  const p2 = { current: { requestId: 'r', condition: 'c' }, selected: 1 }
  assert.equal(goalProposalOnEnter(p2).decision, 'reject')
})

test('escape rejects', () => {
  const p = { current: { requestId: 'r', condition: 'c' }, selected: 0 }
  assert.equal(goalProposalOnEscape(p).decision, 'reject')
})

test('settled clears matching only', () => {
  const p = { current: { requestId: 'r', condition: 'c' }, selected: 0 }
  assert.equal(goalProposalOnSettled(p, 'r'), null)
  assert.equal(goalProposalOnSettled(p, 'other'), p)
})
