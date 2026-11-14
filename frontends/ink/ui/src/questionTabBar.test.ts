/**
 * 页签条的宽度降级。
 *
 * 探针（probes/card_narrow.ts）实测：40 列时页签条被 `truncate-end`
 * 从尾巴切断，用户看到 `[Submi…` —— 提交入口被截掉，
 * 而「提交」恰恰是用户在确认页唯一要做的事。
 *
 * 调研 §9.7.7 的降级是四档（对齐 CC 的 Ok，full:67744）：
 * 1. 全部显示（含 [Submit]）
 * 2. 只显示当前题的 header
 * 3. 只显示当前题的序号（最窄）
 * 4. 读屏器模式换成分隔文本
 *
 * 关键：**每一档都必须保留 [Submit]** —— 它是操作入口，不是装饰。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { tabBarText, type QuestionPanelState } from './questionPanel.js'

function panel(over: Partial<QuestionPanelState> = {}): QuestionPanelState {
  return {
    requestId: 'r',
    questions: [
      { key: 'a', question: '?', header: 'Database choice' },
      { key: 'b', question: '?', header: 'Region' },
      { key: 'c', question: '?', header: 'Strategy' },
    ],
    currentIndex: 0,
    answers: {},
    textInput: {},
    editingKey: null,
    optionIndex: 0,
    hideSubmitTab: false,
    ...over,
  }
}

function width(s: string): number {
  let w = 0
  for (const ch of s) {
    const cp = ch.codePointAt(0) ?? 0
    const wide =
      (cp >= 0x1100 && cp <= 0x115f) ||
      (cp >= 0x2e80 && cp <= 0xa4cf) ||
      (cp >= 0xac00 && cp <= 0xd7a3) ||
      (cp >= 0xf900 && cp <= 0xfaff) ||
      (cp >= 0xfe30 && cp <= 0xfe6f) ||
      (cp >= 0xff00 && cp <= 0xff60) ||
      (cp >= 0xffe0 && cp <= 0xffe6)
    w += wide ? 2 : 1
  }
  return w
}

test('wide terminal shows every tab plus Submit', () => {
  const text = tabBarText(panel(), 120)
  assert.match(text, /Database choice/)
  assert.match(text, /Region/)
  assert.match(text, /Strategy/)
  assert.match(text, /\[Submit\]/)
})

test('medium terminal still shows Submit', () => {
  const text = tabBarText(panel(), 45)
  assert.match(text, /\[Submit\]/, `45 列必须保留 Submit，实际：${text}`)
})

test('narrow terminal keeps Submit and the current header', () => {
  const text = tabBarText(panel(), 30)
  assert.match(text, /\[Submit\]/, `30 列必须保留 Submit，实际：${text}`)
  assert.match(text, /Database/, `30 列要保留当前题 header，实际：${text}`)
})

test('very narrow terminal still keeps Submit', () => {
  const text = tabBarText(panel(), 20)
  assert.match(text, /Submit/, `20 列必须保留 Submit，实际：${text}`)
})

test('text never exceeds the given width', () => {
  for (const cols of [120, 80, 60, 45, 40, 30, 20]) {
    const text = tabBarText(panel(), cols)
    assert.ok(
      width(text) <= cols,
      `${cols} 列下宽度 ${width(text)} 超了：${text}`,
    )
  }
})

test('hideSubmitTab drops the Submit tab', () => {
  const text = tabBarText(panel({ hideSubmitTab: true }), 120)
  assert.doesNotMatch(text, /\[Submit\]/)
})

test('answered questions are marked', () => {
  const text = tabBarText(panel({ answers: { a: 'x' } }), 120)
  assert.match(text, /\[x\]/)
})

test('the current question is marked', () => {
  const text = tabBarText(panel({ currentIndex: 1 }), 120)
  assert.match(text, />/)
})

test('narrow terminal still identifies the current question', () => {
  // 最窄档空间不够时放弃 header，但仍要让用户知道在哪一题：
  // 保留当前题的 header，或退化成序号。
  const text = tabBarText(panel({ currentIndex: 1 }), 20)
  assert.match(
    text,
    /Region|\[ \]2|>\[ \]2/,
    `20 列必须能看出当前在第 2 题，实际：${text}`,
  )
})
