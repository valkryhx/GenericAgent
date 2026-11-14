import test from 'node:test'
import assert from 'node:assert/strict'
import {
  answersToPayload,
  applyAnswer,
  allQuestionsAnswered,
  createQuestionPanel,
  currentQuestionAt,
  hideSubmitTab,
  isReviewPage,
  lastPageIndex,
  movePage,
  questionFromRequest,
  shouldAutoSubmit,
  unansweredKeys,
  type QuestionPanelState,
} from './questionPanel.js'

const QUESTIONS = [
  { key: 'q1', question: 'Which DB?', header: 'DB', options: [{ label: 'postgres' }, { label: 'sqlite' }] },
  { key: 'q2', question: 'Which region?', header: 'Region', options: [{ label: 'us' }, { label: 'eu' }] },
]

test('questionFromRequest maps the payload', () => {
  const req = questionFromRequest({
    type: 'question_request',
    requestId: 'r1',
    questions: QUESTIONS,
  } as never)
  assert.equal(req.requestId, 'r1')
  assert.equal(req.questions.length, 2)
  assert.equal(req.questions[0].key, 'q1')
})

test('createQuestionPanel starts on the first question with no answers', () => {
  const p = createQuestionPanel(questionFromRequest({
    type: 'question_request', requestId: 'r1', questions: QUESTIONS,
  } as never))
  assert.equal(p.currentIndex, 0)
  assert.deepEqual(p.answers, {})
  assert.equal(p.hideSubmitTab, false)
})

test('hideSubmitTab is true only for a single non-multiselect question', () => {
  assert.equal(hideSubmitTab([{ key: 'q1', question: '?' }]), true)
  assert.equal(hideSubmitTab([{ key: 'q1', question: '?', multiSelect: true }]), false)
  assert.equal(hideSubmitTab(QUESTIONS), false)
})

test('lastPageIndex points at the review page unless the tab is hidden', () => {
  // 2 questions, submit tab visible → pages 0,1 are questions, page 2 is review
  assert.equal(lastPageIndex(QUESTIONS, false), 2)
  // single non-multiselect → only page 0
  assert.equal(lastPageIndex([{ key: 'q1', question: '?' }], true), 0)
})

test('isReviewPage is true at index === questions.length', () => {
  assert.equal(isReviewPage(QUESTIONS, 0), false)
  assert.equal(isReviewPage(QUESTIONS, 1), false)
  assert.equal(isReviewPage(QUESTIONS, 2), true)
})

test('currentQuestionAt returns the question, or null on the review page', () => {
  assert.equal(currentQuestionAt(QUESTIONS, 0)?.key, 'q1')
  assert.equal(currentQuestionAt(QUESTIONS, 2), null)
})

test('movePage clamps within the allowed range', () => {
  const p: QuestionPanelState = {
    ...createQuestionPanel(questionFromRequest({
      type: 'question_request', requestId: 'r1', questions: QUESTIONS,
    } as never)),
  }
  assert.equal(movePage(p, -1).currentIndex, 0)
  assert.equal(movePage(p, 1).currentIndex, 1)
  assert.equal(movePage({ ...p, currentIndex: 2 }, 1).currentIndex, 2)
})

test('applyAnswer records a single-select answer as a string', () => {
  const p = createQuestionPanel(questionFromRequest({
    type: 'question_request', requestId: 'r1', questions: QUESTIONS,
  } as never))
  const next = applyAnswer(p, 'q1', 'postgres')
  assert.equal(next.answers.q1, 'postgres')
})

test('applyAnswer records a multiselect answer as a list', () => {
  const qs = [{ key: 'q1', question: '?', multiSelect: true, options: [{ label: 'a' }, { label: 'b' }] }]
  const p = createQuestionPanel(questionFromRequest({
    type: 'question_request', requestId: 'r1', questions: qs,
  } as never))
  const next = applyAnswer(p, 'q1', ['a', 'b'])
  assert.deepEqual(next.answers.q1, ['a', 'b'])
})

test('applyAnswer ignores a key that was not asked', () => {
  const p = createQuestionPanel(questionFromRequest({
    type: 'question_request', requestId: 'r1', questions: QUESTIONS,
  } as never))
  const next = applyAnswer(p, 'q_hacked', 'x')
  assert.deepEqual(next.answers, {})
})

test('allQuestionsAnswered ignores empty strings', () => {
  assert.equal(allQuestionsAnswered(QUESTIONS, {}), false)
  assert.equal(allQuestionsAnswered(QUESTIONS, { q1: 'postgres' }), false)
  assert.equal(allQuestionsAnswered(QUESTIONS, { q1: 'postgres', q2: 'eu' }), true)
  // empty string is not an answer — matches CC's `!!qp(...)`
  assert.equal(allQuestionsAnswered(QUESTIONS, { q1: '', q2: 'eu' }), false)
  // empty list is not an answer either
  assert.equal(allQuestionsAnswered(QUESTIONS, { q1: [], q2: 'eu' }), false)
})

test('unansweredKeys lists the ones left blank', () => {
  assert.deepEqual(unansweredKeys(QUESTIONS, { q1: 'postgres' }), ['q2'])
  assert.deepEqual(unansweredKeys(QUESTIONS, {}), ['q1', 'q2'])
  assert.deepEqual(unansweredKeys(QUESTIONS, { q1: 'a', q2: 'b' }), [])
})

test('answersToPayload drops blank answers before submit', () => {
  const payload = answersToPayload({ q1: '', q2: 'eu' })
  assert.deepEqual(payload, { q2: 'eu' })
})

test('shouldAutoSubmit only fires for one question with a scalar answer', () => {
  const single = createQuestionPanel(questionFromRequest({
    type: 'question_request', requestId: 'r1', questions: [{ key: 'q1', question: '?' }],
  } as never))
  assert.equal(shouldAutoSubmit(single, 'postgres'), true)

  // 多题：不自动提交
  const multi = createQuestionPanel(questionFromRequest({
    type: 'question_request', requestId: 'r1', questions: QUESTIONS,
  } as never))
  assert.equal(shouldAutoSubmit(multi, 'postgres'), false)

  // 单题多选：答案形状是数组 → 不自动提交
  const singleMulti = createQuestionPanel(questionFromRequest({
    type: 'question_request',
    requestId: 'r1',
    questions: [{ key: 'q1', question: '?', multiSelect: true }],
  } as never))
  assert.equal(shouldAutoSubmit(singleMulti, ['a']), false)
})
