/**
 * 测量：真实 relay 的 delta 粒度有多大？每次 delta 会让帧高跳多少？
 *
 * 模拟里我用「每次 delta 加 1 行」，但第三方 relay（ecloud）可能一次给多行。
 * 如果一次给 3 行，帧高就可能一次跳 3 行 —— 与用户观察的 +2/+2/+3 吻合。
 */
import { planMessageViewport } from '../src/messageViewportPlan.js'
import { commitStreamingAssistantMessages } from '../src/streamCommit.js'
import type { ChatMessage } from '../src/protocol.js'

const NL = '\n'
const BOTTOM_ROWS = 5
const MESSAGE_ROWS = 34

function simulate(deltaLines: number, label: string) {
  let messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'Q', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: '', done: false, taskId: 1 },
  ]
  const heights: number[] = []
  const jumps: string[] = []
  let prev = 0

  const total = 60
  for (let i = 0; i < total; i += deltaLines) {
    const idx = messages.findIndex(m => m.id === 'a-1')
    if (idx === -1) break
    const chunk = Array.from(
      { length: Math.min(deltaLines, total - i) },
      (_, k) => `line ${i + k}`,
    ).join(NL) + NL
    messages = messages.slice()
    messages[idx] = { ...messages[idx]!, text: messages[idx]!.text + chunk }
    messages = commitStreamingAssistantMessages(messages, 1)

    const liveText = messages.find(m => m.id === 'a-1')?.text ?? ''
    const liveLines = liveText === '' ? 0 : liveText.split(NL).length
    const plan = planMessageViewport({
      hasStaticMessages: true,
      liveLineCount: liveLines,
      messageRows: MESSAGE_ROWS,
      maxLiveRows: MESSAGE_ROWS,
      keepLivePlaceholder: true,
    })
    const h = (plan.kind === 'none' ? 0 : plan.height) + BOTTOM_ROWS
    heights.push(h)
    if (prev && h !== prev) jumps.push(`${prev}->${h}`)
    prev = h
  }

  const min = Math.min(...heights)
  const max = Math.max(...heights)
  const jumpSizes = jumps.map(j => {
    const [a, b] = j.split('->').map(Number)
    return Math.abs(b! - a!)
  })
  console.log(
    `${label.padEnd(20)} 帧高 ${String(min).padStart(2)}~${String(max).padStart(2)}`
    + `  跳变 ${String(jumps.length).padStart(2)} 次`
    + `  最大 ${Math.max(...jumpSizes, 0)} 行`
    + `  跳变序列前6: ${jumps.slice(0, 6).join(' ')}`,
  )
}

console.log('总量固定 60 行，改变 delta 粒度\n')
simulate(1, 'delta=1 行/次')
simulate(2, 'delta=2 行/次')
simulate(3, 'delta=3 行/次')
simulate(6, 'delta=6 行/次')
simulate(10, 'delta=10 行/次')
