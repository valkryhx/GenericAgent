/**
 * 前端探针：把 ask_user_question 卡片真正渲染出来，抓屏幕字节。
 *
 * 为什么需要探针而不是单测：
 * 单测只验证状态机的输入输出，看不到**用户实际看到的东西**。
 * 卡片有没有溢出、页签条会不会撑破宽度、光标在不在、中文会不会错位——
 * 这些只有真渲染才知道。
 *
 * 用法：npx tsx probes/question_card.ts
 * 产出：tests/probes/captures/question_card.*.txt（屏幕快照）
 */
import { EventEmitter } from 'node:events'
import React from 'react'
import { render, Box, Text } from 'ink'
import {
  createQuestionPanel,
  questionFromRequest,
  optionRows,
  isReviewPage,
  currentQuestionAt,
  unansweredKeys,
  beginTextInput,
  type QuestionPanelState,
  type QuestionSpec,
} from '../src/questionPanel.js'
import type { InkTheme } from '../src/theme.js'

const NL = String.fromCharCode(10)

/** 最小可用的假主题：只要颜色字段存在即可。 */
const theme = {
  accent: 'cyan',
  muted: 'gray',
  warning: 'yellow',
  success: 'green',
  error: 'red',
} as unknown as InkTheme

/**
 * 一个只记录写入、不做真实终端控制的 stream。
 * 我们只要看「Ink 想往屏幕上写什么」，不需要 ANSI 光标移动的完整语义。
 */
class CaptureStream extends EventEmitter {
  readonly chunks: string[] = []
  isTTY = true
  columns: number
  rows: number
  constructor(columns: number, rows: number) {
    super()
    this.columns = columns
    this.rows = rows
  }
  write(s: string): boolean {
    this.chunks.push(s)
    return true
  }
  get text(): string {
    return this.chunks.join('')
  }
}

// ---- 与 App.tsx 里 QuestionPanelView 同构的渲染 ----
// 这里刻意复制一份而不是 import：App.tsx 的组件不在 src 里导出，
// 而探针要能看到「同一份 UI 逻辑渲染出来是什么样」。
// 若两者漂移，探针就失去意义 —— 所以下面会核对渲染结果里的关键字。

function QuestionTabBar({ panel }: { panel: QuestionPanelState }) {
  return React.createElement(
    Text,
    { color: theme.muted, wrap: 'truncate-end' },
    panel.questions
      .map((q, index) => {
        const v = panel.answers[q.key]
        const done = Array.isArray(v) ? v.some(x => x !== '') : v !== undefined && v !== ''
        const label = q.header || `Q${index + 1}`
        const here = index === panel.currentIndex
        return `${here ? '>' : ' '}${done ? '[x]' : '[ ]'}${label}`
      })
      .join(' ') +
      (panel.hideSubmitTab
        ? ''
        : ` ${panel.currentIndex === panel.questions.length ? '>' : ' '}[Submit]`),
  )
}

function QuestionCard({ panel }: { panel: QuestionPanelState }) {
  const review = isReviewPage(panel.questions, panel.currentIndex)
  const question = currentQuestionAt(panel.questions, panel.currentIndex)
  const rows = optionRows(panel)
  const children: React.ReactNode[] = []

  children.push(React.createElement(QuestionTabBar, { key: 'tabs', panel }))

  if (review) {
    const missing = unansweredKeys(panel.questions, panel.answers)
    children.push(React.createElement(Text, { key: 'title', bold: true, color: theme.accent }, 'Review your answers'))
    if (missing.length > 0) {
      children.push(
        React.createElement(Text, { key: 'warn', color: theme.warning }, 'You have not answered all questions'),
      )
    }
    for (const q of panel.questions) {
      const value = panel.answers[q.key]
      if (value === undefined || value === '' || (Array.isArray(value) && value.length === 0)) continue
      const text = Array.isArray(value) ? value.join(', ') : value
      children.push(
        React.createElement(Text, { key: `a-${q.key}`, wrap: 'truncate-end' }, `  ${q.question} → ${text}`),
      )
    }
    children.push(React.createElement(Text, { key: 'ready', color: theme.muted }, 'Ready to submit your answers?'))
    children.push(React.createElement(Text, { key: 'hint', color: theme.muted }, 'Enter 提交 · Esc 取消'))
  } else if (question) {
    children.push(React.createElement(Text, { key: 'q', bold: true, color: theme.accent }, question.question))
    rows.forEach((row, index) => {
      const selected = index === panel.optionIndex
      if (row.kind === 'option') {
        const v = panel.answers[question.key]
        const chosen = Array.isArray(v) ? v.includes(row.label) : v === row.label
        const marker = question.multiSelect ? (chosen ? '[x]' : '[ ]') : chosen ? '(o)' : '( )'
        children.push(
          React.createElement(
            Text,
            { key: `o-${row.label}`, color: selected ? theme.accent : undefined, wrap: 'truncate-end' },
            `${selected ? '> ' : '  '}${marker} ${row.label}`,
          ),
        )
      } else if (row.kind === 'other') {
        const typing = panel.editingKey === question.key
        const typed = panel.textInput[question.key] ?? ''
        children.push(
          React.createElement(
            Text,
            { key: 'other', color: selected ? theme.accent : undefined },
            typing ? `  Other: ${typed}▏` : `${selected ? '> ' : '  '}Other (type your own answer)`,
          ),
        )
      } else {
        children.push(
          React.createElement(Text, { key: 'chat', color: selected ? theme.accent : undefined }, `${selected ? '> ' : '  '}Chat about this`),
        )
      }
    })
    children.push(
      React.createElement(
        Text,
        { key: 'hint', color: theme.muted },
        `Enter 选择 · Up/Down 移动 · Esc 取消 · 第 ${panel.currentIndex + 1}/${panel.questions.length} 题`,
      ),
    )
  }

  return React.createElement(Box, { flexDirection: 'column', paddingX: 1, flexShrink: 0 }, children)
}

// ---- 场景 ----

const MULTI: QuestionSpec[] = [
  {
    key: 'db',
    question: 'Which database should the migration target?',
    header: 'DB',
    options: [{ label: 'postgres' }, { label: 'sqlite' }, { label: 'mysql' }],
  },
  {
    key: 'region',
    question: 'Which region?',
    header: 'Region',
    multiSelect: true,
    options: [{ label: 'us-east-1' }, { label: 'eu-west-1' }],
  },
]

const SINGLE: QuestionSpec[] = [
  {
    key: 'db',
    question: 'Which database?',
    header: 'DB',
    options: [{ label: 'postgres' }, { label: 'sqlite' }],
  },
]

function mkPanel(questions: QuestionSpec[]): QuestionPanelState {
  return createQuestionPanel(
    questionFromRequest({ type: 'question_request', requestId: 'r1', questions } as never),
  )
}

const scenarios: Array<[string, QuestionPanelState]> = [
  ['A-multi-first-question', mkPanel(MULTI)],
  ['B-multi-answered-first', { ...mkPanel(MULTI), answers: { db: 'postgres' } }],
  ['C-multi-review-incomplete', { ...mkPanel(MULTI), currentIndex: 2, answers: { db: 'postgres' } }],
  ['D-multi-review-complete', { ...mkPanel(MULTI), currentIndex: 2, answers: { db: 'postgres', region: ['us-east-1'] } }],
  ['E-single-no-submit-tab', mkPanel(SINGLE)],
  ['F-single-typing-other', beginTextInput(mkPanel(SINGLE), 'db')],
  ['G-multi-typing-other', { ...beginTextInput(mkPanel(MULTI), 'db'), textInput: { db: 'we use oracle' } }],
]

async function capture(name: string, panel: QuestionPanelState, columns: number): Promise<string> {
  const stdout = new CaptureStream(columns, 40)
  const instance = render(React.createElement(QuestionCard, { panel }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
  })
  await new Promise(r => setTimeout(r, 120))
  instance.unmount()
  await new Promise(r => setTimeout(r, 80))

  // debug 模式下 Ink 把每帧都写出来；取最后一段可见文本
  const raw = stdout.text
  const lines = raw
    .split(/\r?\n/)
    .map(l => l.replace(/\x1b\[[0-9;]*[A-Za-z]/g, '')) // 去 ANSI 颜色
    .filter(l => l.trim() !== '')
  return lines.slice(-40).join(NL)
}

async function main() {
  const results: string[] = []
  for (const [name, panel] of scenarios) {
    for (const columns of [120, 60]) {
      const screen = await capture(name, panel, columns)
      results.push(`===== ${name} @${columns} cols =====`)
      results.push(screen)
      results.push('')
    }
  }
  const out = results.join(NL)
  process.stdout.write(out)

  const fs = await import('node:fs')
  const path = await import('node:path')
  // 相对本文件定位，不依赖 cwd：probes/ → ui/ → ink/ → frontends/ → 仓库根
  const dir = path.resolve(import.meta.dirname, '../../../../tests/probes/captures')
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, 'question_card.txt'), out, 'utf8')
  process.stderr.write(`wrote ${path.join(dir, 'question_card.txt')}${NL}`)
}

main().catch(e => {
  console.error(e)
  process.exit(1)
})
