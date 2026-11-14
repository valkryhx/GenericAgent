/**
 * 探针：超宽内容在窄终端下到底会怎样。
 *
 * 上一个探针（card_width.ts）用 44 字符的短句测，60 列当然不用截断 ——
 * 那不是「不降级」，是「不需要降级」。这里用真正超宽的内容重测，
 * 并且直接渲染整个卡片（含页签条 + 中文提示行）。
 */
import React from 'react'
import { EventEmitter } from 'node:events'
import { render, Box, Text } from 'ink'
import {
  createQuestionPanel,
  questionFromRequest,
  optionRows,
  currentQuestionAt,
  type QuestionSpec,
} from '../src/questionPanel.js'

const NL = String.fromCharCode(10)

class CaptureStream extends EventEmitter {
  readonly chunks: string[] = []
  isTTY = true
  constructor(public columns: number, public rows: number) {
    super()
  }
  write(s: string): boolean {
    this.chunks.push(s)
    return true
  }
  get text() {
    return this.chunks.join('')
  }
}

const LONG_QUESTIONS: QuestionSpec[] = [
  {
    key: 'db',
    question:
      'Which database should the migration target, considering the fact that we have three production clusters with different replication topologies?',
    header: 'Database choice',
    options: [
      { label: 'postgres — the primary cluster, which already runs 14.2', description: 'no migration needed' },
      { label: 'sqlite — only for the local development sandbox' },
      { label: 'mysql — legacy, being decommissioned next quarter' },
    ],
  },
  { key: 'region', question: 'Which region?', header: 'Region' },
]

function Card({ panel }: { panel: ReturnType<typeof createQuestionPanel> }) {
  const q = currentQuestionAt(panel.questions, panel.currentIndex)
  const rows = optionRows(panel)
  const kids: React.ReactNode[] = [
    React.createElement(
      Text,
      { key: 'tabs', color: 'gray', wrap: 'truncate-end' },
      panel.questions.map((x, i) => `${i === panel.currentIndex ? '>' : ' '}[ ]${x.header || `Q${i + 1}`}`).join(' ') + ' [Submit]',
    ),
  ]
  if (q) {
    kids.push(React.createElement(Text, { key: 'q', bold: true, wrap: 'truncate-end' }, q.question))
    for (const [i, row] of rows.entries()) {
      if (row.kind === 'option') {
        kids.push(
          React.createElement(Text, { key: `r${i}`, wrap: 'truncate-end' }, `  ( ) ${row.label}`),
        )
      }
    }
    kids.push(
      React.createElement(
        Text,
        { key: 'hint', color: 'gray', wrap: 'truncate-end' },
        'Enter 选择 · Up/Down 移动 · Esc 取消 · 第 1/2 题',
      ),
    )
  }
  return React.createElement(Box, { flexDirection: 'column', paddingX: 1 }, kids)
}

/** 显示宽度：CJK 算 2 列。 */
function displayWidth(s: string): number {
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

async function capture(columns: number): Promise<string[]> {
  const stdout = new CaptureStream(columns, 40)
  const panel = createQuestionPanel(
    questionFromRequest({ type: 'question_request', requestId: 'r1', questions: LONG_QUESTIONS } as never),
  )
  const inst = render(React.createElement(Card, { panel }), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    debug: true,
    patchConsole: false,
  })
  await new Promise(r => setTimeout(r, 120))
  inst.unmount()
  await new Promise(r => setTimeout(r, 80))

  // 去 ANSI：包含 `\x1b[?25l` 这类带 ? 的序列（光标隐藏/显示），
  // 漏掉它会把两帧粘在一起，测出的宽度是假的。
  const clean = stdout.text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '')
  const lines = clean.split(/\r?\n/).filter(l => l.trim() !== '')
  // 取最后一帧（Ink debug 会写多帧）
  const tail = lines.slice(-6)
  return tail.map(l => `w=${String(displayWidth(l)).padStart(3)} | ${l}`)
}

async function main() {
  const out: string[] = []
  for (const columns of [120, 80, 60, 40, 30]) {
    out.push(`===== columns=${columns} =====`)
    out.push(...(await capture(columns)))
    out.push('')
  }
  process.stdout.write(out.join(NL) + NL)
  const fs = await import('node:fs')
  const path = await import('node:path')
  const dir = path.resolve(import.meta.dirname, '../../../../tests/probes/captures')
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, 'card_narrow.txt'), out.join(NL), 'utf8')
}

main().catch(e => {
  console.error(e)
  process.exit(1)
})
