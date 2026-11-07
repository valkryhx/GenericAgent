/**
 * 关键测量：文本行数 vs 渲染后的显示行数。
 *
 * 之前所有模拟都用「文本行数」，但 Ink 实际渲染的是 markdown -> 行数组，
 * 一条文本可能渲染成多行（标题、列表、代码块、换行）。
 */
import { formatAssistantText } from '../src/messageFormat.js'

const samples = [
  'line 0\nline 1\nline 2',
  '**bold line**\nplain line',
  '- item a\n- item b\n- item c',
  '```\ncode 1\ncode 2\n```',
  '# heading\nbody text',
  'Summary: 用户要求\n\n1\n2\n3\n4\n5',
  'Worked for <1s\n\n---',
]

console.log('文本'.padEnd(30), '文本行', '渲染行', '放大')
console.log('-'.repeat(56))

for (const text of samples) {
  const rendered = formatAssistantText(text)
  const textLines = text.split('\n').length
  const displayLines = rendered.split('\n').length
  const ratio = (displayLines / Math.max(1, textLines)).toFixed(2)
  const label = JSON.stringify(text).slice(0, 28)
  console.log(
    `${label.padEnd(30)} ${String(textLines).padStart(4)} ${String(displayLines).padStart(6)} ${ratio.padStart(6)}x`,
  )
}
