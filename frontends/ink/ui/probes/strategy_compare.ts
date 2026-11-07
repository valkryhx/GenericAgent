/**
 * 决定性测量：三种策略下「整帧高度」的波动幅度。
 *
 * 策略：
 *   A. 现状：tail=8，每次超就提交（live 在 8↔9 摆，但提交 32 次）
 *   B. 批量：tail=8, batch=8（live 在 8↔16 摆，提交 10 次）
 *   C. 固定槽位 + tail=槽位（live 不超过槽位，帧高度应恒定）
 *
 * 判据：整帧行数的 min/max 与「跳变幅度」。跳变越大用户越容易察觉。
 */
import { commitStreamingAssistantMessages, DEFAULT_STREAM_LIVE_TAIL_LINES } from '../src/streamCommit.js'
import { planMessageViewport } from '../src/messageViewportPlan.js'
import type { ChatMessage } from '../src/protocol.js'

const NL = '\n'
const BOTTOM_ROWS = 5
const MESSAGE_ROWS = 34

function simulate(tailLines: number, batchLines: number, label: string, fixedBand?: number) {
  let messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'Q', done: true, taskId: 1 },
    { id: 'a-1', role: 'assistant', text: '', done: false, taskId: 1 },
  ]

  const heights: number[] = []
  let commits = 0

  // 模拟 60 个 delta，每个 1 行
  for (let i = 0; i < 60; i += 1) {
    const idx = messages.findIndex(m => m.id === 'a-1')
    if (idx === -1) break
    messages = messages.slice()
    messages[idx] = { ...messages[idx]!, text: messages[idx]!.text + `line ${i}` + NL }

    const before = messages.filter(m => m.id.startsWith('a-1-c')).length
    messages = commitStreamingAssistantMessages(messages, 1, tailLines, batchLines)
    if (messages.filter(m => m.id.startsWith('a-1-c')).length > before) commits += 1

    const liveText = messages.find(m => m.id === 'a-1')?.text ?? ''
    const liveLines = liveText === '' ? 0 : liveText.split(NL).length

    const plan = fixedBand !== undefined
      ? { kind: 'live' as const, height: fixedBand }
      : planMessageViewport({
        hasStaticMessages: true,
        liveLineCount: liveLines,
        messageRows: MESSAGE_ROWS,
        maxLiveRows: MESSAGE_ROWS,
        keepLivePlaceholder: true,
      })

    const vpHeight = plan.kind === 'none' ? 0 : plan.height
    heights.push(vpHeight + BOTTOM_ROWS)
  }

  const min = Math.min(...heights)
  const max = Math.max(...heights)
  const jumps = heights.slice(1).map((h, i) => Math.abs(h - heights[i]!))
  const bigJumps = jumps.filter(j => j >= 2).length
  const maxJump = Math.max(...jumps, 0)

  console.log(
    `${label.padEnd(30)} 帧高 ${String(min).padStart(2)}~${String(max).padStart(2)}`
    + `  提交 ${String(commits).padStart(2)} 次`
    + `  ≥2行跳变 ${String(bigJumps).padStart(2)} 次`
    + `  最大跳变 ${maxJump} 行`,
  )
}

console.log('60 个 delta（每次 1 行），消息区 34 行\n')
simulate(DEFAULT_STREAM_LIVE_TAIL_LINES, 0, 'A 现状 tail=8 不批量')
simulate(DEFAULT_STREAM_LIVE_TAIL_LINES, 8, 'B 批量 tail=8 batch=8')
simulate(DEFAULT_STREAM_LIVE_TAIL_LINES, 0, 'C 固定槽位8 + tail=8', 8)
// D：批量 + 槽位 = tail + batch（槽位刚好盖住 live 的摆动区间）
simulate(8, 8, 'D 批量8 + 固定槽位16', 16)
simulate(4, 4, 'D2 批量4 + 固定槽位8', 8)
simulate(6, 6, 'D3 批量6 + 固定槽位12', 12)
