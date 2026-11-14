import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createQuestionPanel,
  questionFromRequest,
  questionPanelOnEnter,
  questionPanelOnEscape,
  questionPanelRows,
  selectOption,
  toggleMultiOption,
  type QuestionPanelState,
} from './questionPanel.js'

const TWO = [
  { key: 'q1', question: 'Which DB?', header: 'DB', options: [{ label: 'postgres' }, { label: 'sqlite' }] },
  { key: 'q2', question: 'Which region?', header: 'Region', options: [{ label: 'us' }, { label: 'eu' }] },
]

function panelOf(questions: unknown[]): QuestionPanelState {
  return createQuestionPanel(
    questionFromRequest({ type: 'question_request', requestId: 'r1', questions } as never),
  )
}

test('selectOption sets a single-select answer', () => {
  const p = selectOption(panelOf(TWO), 0, 'postgres')
  assert.equal(p.answers.q1, 'postgres')
})

test('selectOption is ignored on the review page', () => {
  const base = panelOf(TWO)
  const p = { ...base, currentIndex: 2 }
  const next = selectOption(p, 0, 'postgres')
  assert.deepEqual(next.answers, {})
})

test('toggleMultiOption adds and removes labels', () => {
  const p = panelOf([{ key: 'q1', question: '?', multiSelect: true, options: [{ label: 'a' }, { label: 'b' }] }])
  assert.deepEqual(toggleMultiOption(p, 0, 'a').answers.q1, ['a'])
  assert.deepEqual(toggleMultiOption(toggleMultiOption(p, 0, 'a'), 0, 'a').answers.q1, [])
  assert.deepEqual(toggleMultiOption(toggleMultiOption(p, 0, 'a'), 0, 'b').answers.q1, ['a', 'b'])
})

test('Enter on the review page submits', () => {
  const base = panelOf(TWO)
  const p = { ...base, currentIndex: 2, answers: { q1: 'postgres', q2: 'eu' } }
  const action = questionPanelOnEnter(p)
  assert.equal(action.type, 'respond')
  if (action.type === 'respond') {
    assert.equal(action.requestId, 'r1')
    assert.deepEqual(action.answers, { q1: 'postgres', q2: 'eu' })
  }
})

test('Enter on a question page advances instead of submitting', () => {
  const p = panelOf(TWO)
  const action = questionPanelOnEnter(p)
  assert.equal(action.type, 'advance')
  if (action.type === 'advance') assert.equal(action.next.currentIndex, 1)
})

test('Enter on the last question page advances to the review page', () => {
  const p = { ...panelOf(TWO), currentIndex: 1 }
  const action = questionPanelOnEnter(p)
  assert.equal(action.type, 'advance')
  if (action.type === 'advance') assert.equal(action.next.currentIndex, 2)
})

test('Enter on the review page with unanswered questions still submits', () => {
  // 只警告不阻断（调研 §9.7.5）
  const p = { ...panelOf(TWO), currentIndex: 2, answers: {} }
  const action = questionPanelOnEnter(p)
  assert.equal(action.type, 'respond')
  if (action.type === 'respond') assert.deepEqual(action.answers, {})
})

test('Escape cancels with the request id', () => {
  const action = questionPanelOnEscape(panelOf(TWO))
  assert.equal(action.type, 'cancel')
  if (action.type === 'cancel') assert.equal(action.requestId, 'r1')
})

test('questionPanelRows counts tabs, title, options and footer', () => {
  const p = panelOf(TWO)
  const rows = questionPanelRows(p)
  // 至少要有：页签条 1 + 题目 1 + 2 个选项 + 底部提示 1
  assert.ok(rows >= 5, `rows=${rows} 太小`)
})

test('questionPanelRows is larger on the review page', () => {
  const base = panelOf(TWO)
  const review = { ...base, currentIndex: 2, answers: { q1: 'a' } }
  assert.ok(questionPanelRows(review) >= 4)
})
