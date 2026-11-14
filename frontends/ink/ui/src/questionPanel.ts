import type { BridgeEvent } from './protocol.js'

/**
 * 结构化提问卡片：多题页签 + 单题表单 + 确认页。
 *
 * 与 approvalPanel 分开而不是复用（实施方案 §7.2）：
 * approvalPanel 的模型是「一条请求 → accept/deny」，这里要承载
 * 「多题 × 多选 × 可输入 × 可只写备注」。硬塞会让两边的语义都分叉。
 *
 * ## 页索引模型
 *
 * 索引 `0..n-1` 是题目，索引 `n` 是确认页 —— 直接复用 CC 的做法
 * （`vr = Mn === nt.length`，调研 §9.7.1），少一个状态变量就少一处不一致。
 *
 * 单题且非多选时隐藏确认页（`hideSubmitTab`），选中即提交。
 * 注意这与「要不要自动提交」是**两套独立判断**：这里看 schema 的
 * `multiSelect` 字段，自动提交看运行时答案是不是数组（调研 §9.7.2）。
 */

export type QuestionOption = {
  label: string
  description?: string
}

export type QuestionSpec = {
  key: string
  question: string
  header?: string
  multiSelect?: boolean
  options?: QuestionOption[]
}

export type QuestionRequest = {
  requestId: string
  questions: QuestionSpec[]
}

export type AnswerValue = string | string[]

export type QuestionPanelState = {
  requestId: string
  questions: QuestionSpec[]
  currentIndex: number
  answers: Record<string, AnswerValue>
  /** 当前题正在输入的文本（Other 行）。 */
  textInput: Record<string, string>
  /** 正在往哪一题输入文本；null = 不在输入态。 */
  editingKey: string | null
  /** 当前题上的选项光标。 */
  optionIndex: number
  hideSubmitTab: boolean
}

export function questionFromRequest(
  event: Extract<BridgeEvent, { type: 'question_request' }>,
): QuestionRequest {
  const raw = (event as { questions?: unknown }).questions
  const list = Array.isArray(raw) ? raw : []
  return {
    requestId: String((event as { requestId?: string }).requestId || ''),
    questions: list
      .filter((q): q is Record<string, unknown> => !!q && typeof q === 'object')
      .map(q => ({
        key: String(q.key || ''),
        question: String(q.question || ''),
        header: q.header === undefined ? undefined : String(q.header),
        multiSelect: q.multiSelect === true,
        options: Array.isArray(q.options)
          ? q.options
              .filter((o): o is Record<string, unknown> => !!o && typeof o === 'object')
              .map(o => ({
                label: String(o.label || ''),
                description: o.description === undefined ? undefined : String(o.description),
              }))
          : [],
      }))
      .filter(q => q.key !== ''),
  }
}

/** 单题且非多选 → 隐藏确认页，选中即提交。 */
export function hideSubmitTab(questions: QuestionSpec[]): boolean {
  if (questions.length !== 1) return false
  return questions[0]?.multiSelect !== true
}

export function createQuestionPanel(request: QuestionRequest): QuestionPanelState {
  return {
    requestId: request.requestId,
    questions: request.questions,
    currentIndex: 0,
    answers: {},
    textInput: {},
    editingKey: null,
    optionIndex: 0,
    hideSubmitTab: hideSubmitTab(request.questions),
  }
}

/** 页索引 === 题目数 → 这是确认页，不是第 N+1 题。 */
export function isReviewPage(questions: QuestionSpec[], index: number): boolean {
  return index === questions.length
}

export function lastPageIndex(questions: QuestionSpec[], tabHidden: boolean): number {
  return tabHidden ? Math.max(0, questions.length - 1) : questions.length
}

export function currentQuestionAt(
  questions: QuestionSpec[],
  index: number,
): QuestionSpec | null {
  if (index < 0 || index >= questions.length) return null
  return questions[index] ?? null
}

export function movePage(panel: QuestionPanelState, delta: number): QuestionPanelState {
  const last = lastPageIndex(panel.questions, panel.hideSubmitTab)
  const next = Math.max(0, Math.min(last, panel.currentIndex + delta))
  if (next === panel.currentIndex) return panel
  return { ...panel, currentIndex: next, optionIndex: 0 }
}

// ---- 选项行：「Other」与「Chat about this」都是 UI 造的，不是模型给的 ----
//
// 提示词明确要求模型不要给 Other（调研 §9.4 / §11.7），因为它是 UI 无条件
// 提供的。Chat 行同理：它把「我不想选，我想跟你聊」建模成一条正式出口，
// 而不是让用户被迫二选一（调研 §9.3）。

export type OptionRow =
  | { kind: 'option'; label: string; description?: string }
  | { kind: 'other' }
  | { kind: 'chat' }

export function optionRows(panel: QuestionPanelState): OptionRow[] {
  if (isReviewPage(panel.questions, panel.currentIndex)) return []
  const question = currentQuestionAt(panel.questions, panel.currentIndex)
  if (!question) return []

  const rows: OptionRow[] = (question.options ?? []).map(o => ({
    kind: 'option' as const,
    label: o.label,
    description: o.description,
  }))
  rows.push({ kind: 'other' })
  // 单题时没有「聊一聊」的必要：没有别的问题可聊，答案就在眼前。
  if (panel.questions.length > 1) rows.push({ kind: 'chat' })
  return rows
}

export function moveOptionSelection(panel: QuestionPanelState, delta: number): number {
  const total = optionRows(panel).length
  if (total === 0) return 0
  return Math.max(0, Math.min(total - 1, panel.optionIndex + delta))
}

/**
 * 记录一个答案。
 *
 * key 必须是问过的题 —— 与后端 `admit` 的规则一致，两边都拦一次。
 * 单选的 `value` 是字符串，多选是字符串数组。
 */
export function applyAnswer(
  panel: QuestionPanelState,
  key: string,
  value: AnswerValue,
): QuestionPanelState {
  if (!panel.questions.some(q => q.key === key)) return panel
  return { ...panel, answers: { ...panel.answers, [key]: value } }
}

export function setTextInput(
  panel: QuestionPanelState,
  key: string,
  text: string,
): QuestionPanelState {
  return { ...panel, textInput: { ...panel.textInput, [key]: text } }
}

/** 进入「Other」文本输入态。 */
export function beginTextInput(
  panel: QuestionPanelState,
  key: string,
): QuestionPanelState {
  return { ...panel, editingKey: key }
}

/** 退出文本输入态（Esc / Enter 之后）。 */
export function endTextInput(panel: QuestionPanelState): QuestionPanelState {
  return { ...panel, editingKey: null }
}

/**
 * 把 Other 行输入的文字转成答案。
 *
 * 空白输入不算答 —— 用户点了 Other 又什么都没写，与没答等价
 * （后端 `finalize` 与这里判定一致）。
 */
export function answerFromTextInput(
  panel: QuestionPanelState,
  key: string,
): QuestionPanelState {
  const text = (panel.textInput[key] ?? '').trim()
  if (!text) return panel
  return applyAnswer(panel, key, text)
}

/**
 * 判断一个答案算不算「回答过」。
 *
 * 空串 / 空数组都不算 —— 对齐 CC 的 `!!qp(...)`（调研 §9.7.5）：
 * 用户点了 Other 但什么都没输，不该被当成已答。
 */
function hasAnswer(value: AnswerValue | undefined): boolean {
  if (value === undefined) return false
  if (Array.isArray(value)) return value.some(v => v !== '')
  return value !== ''
}

export function allQuestionsAnswered(
  questions: QuestionSpec[],
  answers: Record<string, AnswerValue>,
): boolean {
  return questions.every(q => hasAnswer(answers[q.key]))
}

export function unansweredKeys(
  questions: QuestionSpec[],
  answers: Record<string, AnswerValue>,
): string[] {
  return questions.filter(q => !hasAnswer(answers[q.key])).map(q => q.key)
}

/** 提交前把空答案剔掉。后端的 `finalize` 会再做一次。 */
export function answersToPayload(
  answers: Record<string, AnswerValue>,
): Record<string, AnswerValue> {
  const out: Record<string, AnswerValue> = {}
  for (const [key, value] of Object.entries(answers)) {
    if (hasAnswer(value)) out[key] = value
  }
  return out
}

/**
 * 单题单选的快捷路径：答完立即提交，不进确认页。
 *
 * 两个条件来自不同来源（调研 §9.7.3 / §9.7.2）：
 * - `hideSubmitTab`：schema 层，为真即蕴含「恰好 1 题且非多选」；
 * - 答案是标量而不是数组：**运行时**答案形状。
 *
 * 不额外写 `questions.length === 1`——那已经被 `hideSubmitTab` 蕴含，
 * 写了就是死逻辑（实测是一条无法被测试抓住的等价变异）。
 */
export function shouldAutoSubmit(
  panel: QuestionPanelState,
  value: AnswerValue,
): boolean {
  if (!panel.hideSubmitTab) return false
  return !Array.isArray(value)
}

// ---- 键盘动作（纯函数，便于单测；App.tsx 只负责发消息） ----

export type QuestionPanelAction =
  | { type: 'advance'; next: QuestionPanelState }
  | { type: 'respond'; requestId: string; answers: Record<string, AnswerValue> }
  | { type: 'cancel'; requestId: string }
  | { type: 'editText'; key: string }
  | { type: 'chat' }
  | { type: 'noop' }

/** 单选某题：记答案，停在当前页。确认页上不做任何事。 */
export function selectOption(
  panel: QuestionPanelState,
  questionIndex: number,
  label: string,
): QuestionPanelState {
  if (isReviewPage(panel.questions, panel.currentIndex)) return panel
  const question = panel.questions[questionIndex]
  if (!question) return panel
  return applyAnswer(panel, question.key, label)
}

/**
 * 多选某题：把 label 加进 / 移出数组。
 *
 * 与单选的差别不只是「数组」：多选**不会**因为选了一项就推进，
 * 用户需要显式按 Enter 才翻页（调研 §9.7.3）。
 */
export function toggleMultiOption(
  panel: QuestionPanelState,
  questionIndex: number,
  label: string,
): QuestionPanelState {
  if (isReviewPage(panel.questions, panel.currentIndex)) return panel
  const question = panel.questions[questionIndex]
  if (!question) return panel
  const current = panel.answers[question.key]
  const list = Array.isArray(current) ? current : []
  const next = list.includes(label) ? list.filter(v => v !== label) : [...list, label]
  return applyAnswer(panel, question.key, next)
}

/**
 * Enter 的语义随位置变化：
 * - 确认页 → 提交**全部**答案
 * - 选项行 → 选中该项（单选）或切换该项（多选），再决定要不要推进
 * - Other 行 → 进入文本输入，不算作答
 * - Chat 行 → 走「聊一聊」出口（deny + feedback，不伪造答案）
 *
 * 「确认页未答全也允许提交」是刻意的（调研 §9.7.5）：
 * 强行拦住会逼出「随便选一个」。
 */
export function questionPanelOnEnter(panel: QuestionPanelState): QuestionPanelAction {
  if (isReviewPage(panel.questions, panel.currentIndex)) {
    return {
      type: 'respond',
      requestId: panel.requestId,
      answers: answersToPayload(panel.answers),
    }
  }

  const question = currentQuestionAt(panel.questions, panel.currentIndex)
  if (!question) return { type: 'noop' }

  const row = optionRows(panel)[panel.optionIndex]

  if (row?.kind === 'other') {
    return { type: 'editText', key: question.key }
  }
  if (row?.kind === 'chat') {
    return { type: 'chat' }
  }

  if (row?.kind === 'option') {
    if (question.multiSelect) {
      // 多选：切换，不推进 —— 用户要显式按 Enter（在最后一项上）才继续
      const next = toggleMultiOption(panel, panel.currentIndex, row.label)
      return { type: 'advance', next }
    }
    const answered = selectOption(panel, panel.currentIndex, row.label)
    // 单题单选 → 选中即提交（hideSubmitTab 蕴含的就是这个场景）
    if (shouldAutoSubmit(answered, row.label)) {
      return {
        type: 'respond',
        requestId: answered.requestId,
        answers: answersToPayload(answered.answers),
      }
    }
    return { type: 'advance', next: movePage(answered, 1) }
  }

  // 光标停在已输入的 Other 文本上：把它当成答案
  const typed = panel.textInput[question.key]
  if (typed) {
    const answered = applyAnswer(panel, question.key, typed)
    if (shouldAutoSubmit(answered, typed)) {
      return {
        type: 'respond',
        requestId: answered.requestId,
        answers: answersToPayload(answered.answers),
      }
    }
    return { type: 'advance', next: movePage(answered, 1) }
  }

  const next = movePage(panel, 1)
  if (next === panel) {
    return {
      type: 'respond',
      requestId: panel.requestId,
      answers: answersToPayload(panel.answers),
    }
  }
  return { type: 'advance', next }
}

/** Esc = 取消，走普通 deny 语义（调研 §9.4），不伪造答案。 */
export function questionPanelOnEscape(panel: QuestionPanelState): QuestionPanelAction {
  return { type: 'cancel', requestId: panel.requestId }
}

/** 估算卡片占用的行数，供布局预留。 */
export function questionPanelRows(panel: QuestionPanelState): number {
  const base = 4 // 页签条 + 标题 + 底部提示 + 余量
  if (isReviewPage(panel.questions, panel.currentIndex)) {
    return base + Math.max(1, panel.questions.length) + 3
  }
  const question = currentQuestionAt(panel.questions, panel.currentIndex)
  const optionCount = question?.options?.length ?? 0
  // 题目行 + 选项 + Other 行 + Chat 行
  return base + 1 + optionCount + 2
}
