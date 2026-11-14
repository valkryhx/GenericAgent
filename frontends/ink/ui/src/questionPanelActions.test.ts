import test from 'node:test'
import assert from 'node:assert/strict'
import {
  createQuestionPanel,
  questionFromRequest,
  questionPanelOnEnter,
  questionPanelOnEscape,
  questionPanelOnSpace,
  questionPanelRows,
  selectOption,
  textFromRawInput,
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

// ---- 真机 bug 回归（2026-11-14 用户实测）----
//
// 用户原话：「用 enter 表示选中 那如何 submit 呢 尤其是可以多选 我似乎没法提交了」
//
// 根因：多选分支只 toggle 不推进，movePage 永远调不到，多选页走不出去。
// 修法按用户建议：空格 = 确认（切换勾选），Enter = 提交/进入下一步。

const MULTI = [
  {
    key: 'q1',
    question: 'Which traits?',
    header: 'Traits',
    multiSelect: true,
    options: [{ label: 'a' }, { label: 'b' }, { label: 'c' }],
  },
]

test('Space toggles a multiSelect option without advancing the page', () => {
  const p = panelOf(MULTI)
  const action = questionPanelOnSpace(p)
  assert.equal(action.type, 'advance')
  if (action.type !== 'advance') return
  assert.deepEqual(action.next.answers.q1, ['a'], '空格应当勾选')
  assert.equal(action.next.currentIndex, 0, '勾选不该翻页')

  // 回到首项再按一次 → 取消勾选
  const back = { ...action.next, optionIndex: 0 }
  const again = questionPanelOnSpace(back)
  assert.equal(again.type, 'advance')
  if (again.type !== 'advance') return
  assert.deepEqual(again.next.answers.q1, [], '空格应当取消勾选')
})

test('Space moves down after toggling a multiSelect option', () => {
  // 连续按空格应当能勾选多个：光标自动下移，否则用户每次都要再按 ↓
  const p = panelOf(MULTI)
  const first = questionPanelOnSpace(p)
  if (first.type !== 'advance') return assert.fail('expected advance')
  const second = questionPanelOnSpace(first.next)
  if (second.type !== 'advance') return assert.fail('expected advance')
  assert.deepEqual(second.next.answers.q1, ['a', 'b'], '连续按空格应勾选两项')
})

test('Enter on a multiSelect page advances to the next page (the bug)', () => {
  const p = { ...panelOf(MULTI), answers: { q1: ['a', 'b'] } }
  const action = questionPanelOnEnter(p)
  assert.equal(action.type, 'advance', 'Enter 在多选页必须是「下一步」，不能只 toggle')
  if (action.type === 'advance') {
    assert.equal(action.next.currentIndex, 1, '单题多选：下一步就是确认页')
    assert.deepEqual(action.next.answers.q1, ['a', 'b'], 'Enter 不该改动已勾选的答案')
  }
})

test('Enter on a multiSelect page with nothing chosen still advances', () => {
  // 不拦截：与「未答全也在确认页允许提交」一致，拦住只会逼出随便勾一个
  const action = questionPanelOnEnter(panelOf(MULTI))
  assert.equal(action.type, 'advance')
  if (action.type === 'advance') assert.equal(action.next.currentIndex, 1)
})

test('Enter on a multiSelect question with several questions goes to the next question', () => {
  const qs = [
    { key: 'q1', question: 'A?', multiSelect: true, options: [{ label: 'x' }, { label: 'y' }] },
    { key: 'q2', question: 'B?', options: [{ label: 'p' }] },
  ]
  const p = { ...panelOf(qs), answers: { q1: ['x'] } }
  const action = questionPanelOnEnter(p)
  assert.equal(action.type, 'advance')
  if (action.type === 'advance') assert.equal(action.next.currentIndex, 1)
})

test('Space on a single-select option selects it (same as Enter)', () => {
  const p = panelOf(TWO)
  const action = questionPanelOnSpace(p)
  assert.equal(action.type, 'advance')
  if (action.type === 'advance') assert.equal(action.next.answers.q1, 'postgres')
})

test('Space is ignored on the review page', () => {
  const p = { ...panelOf(TWO), currentIndex: 2, answers: { q1: 'postgres', q2: 'eu' } }
  assert.equal(questionPanelOnSpace(p).type, 'noop')
})

// ---- Other 行无法输入（真机 bug 之二）----
//
// 根因：App.tsx 在编辑态里读 `input`（composer 的 useState），而提问卡
// 打开时字符根本进不到 composer，`input` 恒为空串，于是打字无效。
// 字符必须取自 `rawInput`（本次按键的真实字节）。

test('textFromRawInput accepts a printable character', () => {
  assert.equal(textFromRawInput('a', { return: false, escape: false }), 'a')
  assert.equal(textFromRawInput('中', { return: false, escape: false }), '中')
})

test('textFromRawInput rejects escape sequences and control keys', () => {
  assert.equal(textFromRawInput('\x1b[A', { return: false, escape: false }), '', '方向键不是文本')
  assert.equal(textFromRawInput('\r', { return: true, escape: false }), '', 'Enter 不是文本')
  assert.equal(textFromRawInput('\x1b', { return: false, escape: true }), '', 'Esc 不是文本')
  assert.equal(textFromRawInput('\x7f', { return: false, escape: false }), '', '退格不是文本')
  assert.equal(textFromRawInput('', { return: false, escape: false }), '')
})

test('textFromRawInput rejects bare control characters', () => {
  // Ctrl+A / Ctrl+H 这类：既不触发 return/escape 标志，也不以 ESC 开头，
  // 但绝不能当成可输入文本打进 Other 行。
  assert.equal(textFromRawInput('\x01', { return: false, escape: false }), '', 'Ctrl+A 不是文本')
  assert.equal(textFromRawInput('\x08', { return: false, escape: false }), '', 'Ctrl+H 不是文本')
  assert.equal(textFromRawInput('\t', { return: false, escape: false }), '', 'Tab 不该进文本')
})

test('textFromRawInput accepts a multi-character paste', () => {
  assert.equal(textFromRawInput('hello', { return: false, escape: false }), 'hello')
})

