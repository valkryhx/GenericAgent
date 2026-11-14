import test from 'node:test'
import assert from 'node:assert/strict'
import { questionFromRequest } from './questionPanel.js'
import type { BridgeEvent } from './protocol.js'

type QuestionEvent = Extract<BridgeEvent, { type: 'question_request' }>

test('question_request is a member of BridgeEvent', () => {
  const event: QuestionEvent = {
    type: 'question_request',
    requestId: 'r1',
    questions: [{ key: 'q1', question: 'Which DB?' }],
  }
  assert.equal(event.type, 'question_request')
})

test('question_request questions carry the full spec shape', () => {
  const event: QuestionEvent = {
    type: 'question_request',
    requestId: 'r1',
    questions: [
      {
        key: 'q1',
        question: 'Which DB?',
        header: 'DB',
        multiSelect: false,
        options: [{ label: 'postgres', description: 'fast' }],
      },
    ],
  }
  const req = questionFromRequest(event)
  assert.equal(req.questions[0].header, 'DB')
  assert.equal(req.questions[0].options?.[0].description, 'fast')
})

test('question_resolved event exists for settle notifications', () => {
  const settled: BridgeEvent = {
    type: 'question_resolved',
    requestId: 'r1',
    outcome: 'submitted',
  }
  assert.equal(settled.type, 'question_resolved')
})
