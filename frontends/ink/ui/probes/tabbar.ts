/**
 * 探针：页签条在各宽度下的实际表现。
 *
 * 背景：card_narrow.ts 显示 40 列时页签条被 `truncate-end` 从中间切断，
 * 用户看到 `[Submi…` —— 提交入口被截掉了。调研 §9.7.7 的降级策略是
 * 「完整 → 只显示当前题 → 全部压到最短」，不是「从尾巴切」。
 *
 * 这个探针只渲染页签条，量化每种宽度下看到了什么。
 */
import React from 'react'
import { EventEmitter } from 'node:events'
import { render, Box, Text } from 'ink'
import { tabBarText, type QuestionSpec } from '../src/questionPanel.js'

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

const QUESTIONS: QuestionSpec[] = [
  { key: 'a', question: '?', header: 'Database choice' },
  { key: 'b', question: '?', header: 'Region' },
  { key: 'c', question: '?', header: 'Strategy' },
]

async function measure(columns: number): Promise<{ shown: string; width: number }> {
  const stdout = new CaptureStream(columns, 10)
  const panel = {
    requestId: 'r',
    questions: QUESTIONS,
    currentIndex: 0,
    answers: {},
    textInput: {},
    editingKey: null,
    optionIndex: 0,
    hideSubmitTab: false,
  }
  const text = tabBarText(panel as never, columns)
  const inst = render(
    React.createElement(Box, { paddingX: 1 }, React.createElement(Text, { wrap: 'truncate-end' }, text)),
    { stdout: stdout as unknown as NodeJS.WriteStream, debug: true, patchConsole: false },
  )
  await new Promise(r => setTimeout(r, 80))
  inst.unmount()
  await new Promise(r => setTimeout(r, 50))
  const clean = stdout.text.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, '')
  const lines = clean.split(/\r?\n/).filter(l => l.trim() !== '')
  const last = lines[lines.length - 1] ?? ''
  return { shown: last, width: displayWidth(last) }
}

async function main() {
  const out: string[] = []
  for (const columns of [120, 80, 70, 60, 50, 40, 30, 20]) {
    const r = await measure(columns)
    out.push(`cols=${String(columns).padStart(3)} w=${String(r.width).padStart(3)} |${r.shown}|`)
  }
  process.stdout.write(out.join(NL) + NL)
  const fs = await import('node:fs')
  const path = await import('node:path')
  const dir = path.resolve(import.meta.dirname, '../../../../tests/probes/captures')
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, 'tabbar.txt'), out.join(NL), 'utf8')
}

main().catch(e => {
  console.error(e)
  process.exit(1)
})
