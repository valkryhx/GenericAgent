/**
 * 压缩期排队的提示必须说对话（2026-11-15）。
 *
 * ## 背景
 *
 * 压缩期输入从「拒绝」改成了「排队」（bridge 侧 `_enqueue_during_compaction`）。
 * 排队复用 mid-turn 那条队列，因为它在 core 的轮次起点 fold point 被 drain ——
 * 那正好是压缩 worker 启动回合的时机。
 *
 * ## 问题
 *
 * 但前端文案沿用了 mid-turn 的措辞：「will be sent at the next tool boundary」。
 * 压缩期**没有** tool boundary 可等：消息是在压缩结束、回合开始时被 drain 的。
 * 用户读到「下一个工具边界」，就会在压缩跑完时以为消息已经发出去了 ——
 * 而实际它要等这一轮真正开始。
 *
 * 这里锁住：压缩期排队用另一句准确的文案；mid-turn 排队的措辞不受影响。
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { applyBridgeEvent, initialState } from './state.js'

test('a prompt queued during compaction says it waits for compaction, not a tool boundary', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, {
    type: 'prompt_queued',
    text: 'my second thought',
    queued: 1,
    reason: 'compaction',
  })
  const message = state.messages.at(-1)!
  assert.equal(message.queuedPrompt, true)
  assert.match(message.text, /compaction/i, `文案没提到压缩：${message.text}`)
  assert.doesNotMatch(
    message.text,
    /tool boundary/i,
    `压缩期不该说「下一个工具边界」：${message.text}`,
  )
})

test('a mid-turn queued prompt keeps the tool-boundary wording', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'queued mid turn', queued: 1 })
  const message = state.messages.at(-1)!
  assert.match(message.text, /tool boundary/i, `mid-turn 文案被改坏了：${message.text}`)
  assert.doesNotMatch(message.text, /compaction/i, `mid-turn 不该提压缩：${message.text}`)
})

test('the queued text is preserved verbatim in both cases', () => {
  for (const reason of [undefined, 'compaction'] as const) {
    let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
    state = applyBridgeEvent(state, {
      type: 'prompt_queued',
      text: 'keep me exactly',
      queued: 1,
      ...(reason ? { reason } : {}),
    })
    assert.ok(
      state.messages.at(-1)!.text.startsWith('keep me exactly'),
      `排队消息丢了原文（reason=${String(reason)}）`,
    )
  }
})

test('a prompt queued by the bridge counts toward the queued-prompt hint', () => {
  // Two different ways a prompt becomes queued:
  //   1. the user typed during a turn -> the App sends `queue_prompt` itself and
  //      records it locally (App.tsx setQueuedPrompts);
  //   2. the bridge accepted a submit and queued it (compaction) -> App gets a
  //      `prompt_queued` event and, before this test, recorded nothing.
  // Case 2 left the bottom hint saying "type to queue / Enter queues" while a
  // message was already waiting -- the user's only evidence was a transcript line
  // they might have scrolled past.
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'waiting one', queued: 1, reason: 'compaction' })
  assert.equal(state.queuedPrompts.length, 1, 'bridge-queued prompt not tracked for the hint')
  assert.equal(state.queuedPrompts[0], 'waiting one')
})

test('locally queued and bridge-queued prompts do not double count', () => {
  // The App adds its own `queue_prompt` drafts, and the bridge echoes an accepted
  // submit. If both paths write the same text, the hint would claim two messages.
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'same text', queued: 1, reason: 'compaction' })
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'same text', queued: 1, reason: 'compaction' })
  assert.equal(state.queuedPrompts.length, 1, 'the same queued prompt was counted twice')
})

test('the queued-prompt list clears when the turn ends', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'prompt_queued', text: 'one', queued: 1, reason: 'compaction' })
  state = applyBridgeEvent(state, { type: 'status', status: 'idle' })
  assert.deepEqual(state.queuedPrompts, [], 'a queued prompt outlived its turn')
})
