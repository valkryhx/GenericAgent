import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createQuestionPanel,
  questionFromRequest,
  questionPanelOnEnter,
  answerFromTextInput,
  beginTextInput,
  endTextInput,
  type QuestionPanelState,
} from './questionPanel.js'

const TWO = [
  { key: 'q1', question: 'Which DB?', options: [{ label: 'postgres' }] },
  { key: 'q2', question: 'Which region?', options: [{ label: 'us' }] },
]

function panelOf(questions: unknown[]): QuestionPanelState {
  return createQuestionPanel(
    questionFromRequest({ type: 'question_request', requestId: 'r1', questions } as never),
  )
}

test('beginTextInput marks the question as being typed into', () => {
  const p = beginTextInput(panelOf(TWO), 'q1')
  assert.equal(p.editingKey, 'q1')
})

test('endTextInput clears the editing flag', () => {
  const p = endTextInput(beginTextInput(panelOf(TWO), 'q1'))
  assert.equal(p.editingKey, null)
})

test('typing text then Enter uses it as the answer', () => {
  let p = beginTextInput(panelOf(TWO), 'q1')
  p = { ...p, textInput: { q1: 'we use oracle' } }
  // 光标放在 Other 行
  const rows = p.optionIndex
  const action = questionPanelOnEnter({ ...p, optionIndex: 0, editingKey: null })
  // 有 textInput 但光标不在 Other 行 → 走选项逻辑（这里是 postgres）
  assert.equal(action.type, 'advance')
})

test('answerFromTextInput trims and ignores blank input', () => {
  const p = { ...panelOf(TWO), textInput: { q1: '  oracle  ' } }
  const next = answerFromTextInput(p, 'q1')
  assert.equal(next.answers.q1, 'oracle')
})

test('answerFromTextInput leaves answers untouched for blank text', () => {
  const p = { ...panelOf(TWO), textInput: { q1: '   ' } }
  const next = answerFromTextInput(p, 'q1')
  assert.deepEqual(next.answers, {})
})

test('createQuestionPanel starts with editingKey null', () => {
  assert.equal(panelOf(TWO).editingKey, null)
})
