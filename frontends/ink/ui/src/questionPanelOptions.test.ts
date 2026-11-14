import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createQuestionPanel,
  moveOptionSelection,
  optionRows,
  questionFromRequest,
  questionPanelOnEnter,
  type QuestionPanelState,
} from './questionPanel.js'

const TWO = [
  { key: 'q1', question: 'Which DB?', header: 'DB', options: [{ label: 'postgres' }, { label: 'sqlite' }] },
]

function panelOf(questions: unknown[]): QuestionPanelState {
  return createQuestionPanel(
    questionFromRequest({ type: 'question_request', requestId: 'r1', questions } as never),
  )
}

test('optionRows lists the options plus the synthetic Other row', () => {
  const rows = optionRows(panelOf(TWO))
  // TWO 只有 1 题 → 没有 Chat 行（没有别的问题可聊）
  assert.deepEqual(
    rows.map(r => r.kind),
    ['option', 'option', 'other'],
  )
  assert.equal(rows[0].kind, 'option')
  if (rows[0].kind === 'option') assert.equal(rows[0].label, 'postgres')
})

test('optionRows adds a Chat row only when there are several questions', () => {
  const rows = optionRows(panelOf([
    { key: 'q1', question: '?', options: [{ label: 'a' }] },
    { key: 'q2', question: '?', options: [{ label: 'b' }] },
  ]))
  assert.deepEqual(rows.map(r => r.kind), ['option', 'other', 'chat'])
})

test('optionRows on a single question has no Chat row', () => {
  // 单题时没有「和 Claude 聊聊这个」的必要——没有别的问题可聊
  const rows = optionRows(panelOf([{ key: 'q1', question: '?', options: [{ label: 'a' }] }]))
  assert.deepEqual(rows.map(r => r.kind), ['option', 'other'])
})

test('moveOptionSelection clamps within rows', () => {
  const p = panelOf(TWO)
  assert.equal(moveOptionSelection(p, -1), 0)
  assert.equal(moveOptionSelection(p, 1), 1)
  const last = optionRows(p).length - 1
  assert.equal(moveOptionSelection({ ...p, optionIndex: last }, 1), last)
})

test('Enter on an option row selects it and advances', () => {
  const p = { ...panelOf(TWO), optionIndex: 0 }
  const action = questionPanelOnEnter(p)
  // 单题非多选 → hideSubmitTab → 直接提交
  assert.equal(action.type, 'respond')
  if (action.type === 'respond') assert.equal(action.answers.q1, 'postgres')
})

test('Enter on the Other row does not answer', () => {
  const p = { ...panelOf(TWO), optionIndex: 2 } // other
  const action = questionPanelOnEnter(p)
  // 进入输入模式，不算作答
  assert.equal(action.type, 'editText')
})

test('Enter on the Chat row cancels with feedback intent', () => {
  const p = panelOf([
    { key: 'q1', question: '?', options: [{ label: 'a' }] },
    { key: 'q2', question: '?', options: [{ label: 'b' }] },
  ])
  const rows = optionRows(p)
  const chatIndex = rows.findIndex(r => r.kind === 'chat')
  const action = questionPanelOnEnter({ ...p, optionIndex: chatIndex })
  assert.equal(action.type, 'chat')
})

test('createQuestionPanel starts with optionIndex 0', () => {
  assert.equal(panelOf(TWO).optionIndex, 0)
})
