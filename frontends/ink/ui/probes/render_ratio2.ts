/**
 * 关键测量：文本行数 vs 渲染后的显示行数。
 *
 * 之前所有模拟都用「文本行数」，但 Ink 实际渲染的是 markdown -> 行数组，
 * 一条文本可能渲染成多行（标题、列表、代码块、换行）。
 * 如果放大倍数 > 1，那么 delta 携带的换行数不足以解释帧高跳变，
 * 要看「渲染行」而不是「文本行」。
 */
import { formatAssistantText } from '../src/messageFormat.ts'

const samples = [
  'line 0\nline 1\nline 2',
  '**bold line**\nplain line',
  '- item a\n- item b\n- item c',
  '```\ncode 1\ncode 2\n```',
  '# heading\nbody text',
  'Summary: 用户要求\n\n1\n2\n3\n4\n5',
  'Worked for <1s\n\n---',
  '介绍终端 UI 渲染的基本原理。终端 UI 的核心是「帧」与「差量」。',
]

console.log('文本'.padEnd(34), '文本行', '渲染行', '放大')
console.log('-'.repeat(60))

let worst = 0
for (const text of samples) {
  const rendered = formatAssistantText(text)
  const textLines = text.split('\n').length
  const displayLines = rendered.split('\n').length
  const ratio = displayLines / Math.max(1, textLines)
  if (ratio > worst) worst = ratio
  const label = JSON.stringify(text).slice(0, 32)
  console.log(
    `${label.padEnd(34)} ${String(textLines).padStart(4)} ${String(displayLines).padStart(6)} ${ratio.toFixed(2).padStart(6)}x`,
  )
}
console.log('')
console.log(`最大放大倍数 ${worst.toFixed(2)}x`)
if (worst > 1.2) {
  console.log('=> 渲染会放大行数：帧高跳变应按【渲染行】而非文本行计算')
} else {
  console.log('=> 渲染基本 1:1，行数不是放大源')
}
