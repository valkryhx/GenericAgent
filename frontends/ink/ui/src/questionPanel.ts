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
  return { ...panel, currentIndex: next }
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
