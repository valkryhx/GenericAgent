import test from 'node:test'
import assert from 'node:assert/strict'
import {
  commitStreamingAssistantMessages,
  committedAssistantPrefix,
  DEFAULT_STREAM_LIVE_TAIL_LINES,
  findFenceSafeOverflowCount,
  remainingAssistantTextAfterCommits,
} from './streamCommit.js'
import type { ChatMessage } from './protocol.js'
import { applyBridgeEvent, initialState } from './state.js'
import { splitStaticAndActiveMessages } from './messagePartition.js'
import { formatAssistantText } from './messageFormat.js'
import { renderMarkdownLines } from './markdownRender.js'

function liveAssistant(taskId: number, text: string): ChatMessage {
  return { id: `a-${taskId}`, role: 'assistant', text, done: false, taskId }
}

test('commitStreamingAssistantMessages leaves short streams untouched', () => {
  const lines = Array.from({ length: 3 }, (_, i) => `line ${i}`).join('\n')
  const messages: ChatMessage[] = [
    { id: 'u-1', role: 'user', text: 'q', done: false, taskId: 1 },
    liveAssistant(1, lines),
  ]
  const next = commitStreamingAssistantMessages(messages, 1, 8)
  assert.equal(next, messages)
  assert.equal(next.filter(m => m.id.startsWith('a-1-c')).length, 0)
  assert.equal(next.find(m => m.id === 'a-1')?.text, lines)
})

test('commitStreamingAssistantMessages does not split inside an open backtick fence', () => {
  // Real GA tool traces wrap [Action]/[Status] in ````` fences. A naive
  // "keep last N lines" cut can leave the fence open in the committed segment
  // and put "**LLM Running (Step 2)**" into the live tail after an unmatched
  // opener — marked then treats Turn 2 as a code block and shows literal **.
  const body = [
    'A0', 'A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7',
    '`````',
    '[Action] Calling MCP tool: mcp__tavily__tavily_search',
    '[Status] MCP success',
    '`````',
    '**LLM Running (Step 2) ...**',
    'tail0',
    'tail1',
    'tail2',
  ].join('\n')
  // Naive cut with maxTail=4 would commit through "[Status]" and leave the
  // closing fence + Turn 2 in the live tail → open fence in commit, Turn 2
  // after opener in live. Fence-safe cut must move the boundary.
  const messages: ChatMessage[] = [liveAssistant(7, body)]
  const next = commitStreamingAssistantMessages(messages, 7, 5)
  const commits = next.filter(m => m.id.startsWith('a-7-c'))
  const live = next.find(m => m.id === 'a-7')
  assert.ok(commits.length >= 1)
  for (const commit of commits) {
    assert.equal(
      fenceBalance(commit.text),
      0,
      `commit ${commit.id} left an open fence:\n${commit.text}`,
    )
  }
  assert.equal(fenceBalance(live?.text ?? ''), 0, `live tail open fence:\n${live?.text}`)
  // Turn 2 header must not sit after an unmatched opening fence in the same segment.
  for (const segment of [...commits, live!]) {
    if (!segment.text.includes('Turn 2')) continue
    const beforeTurn2 = segment.text.slice(0, segment.text.indexOf('Turn 2'))
    assert.equal(fenceBalance(beforeTurn2), 0, `Turn 2 trapped in open fence:\n${segment.text}`)
  }
  // Turn 2 must still appear exactly once across segments.
  const all = [...commits, live!].map(m => m.text).join('\n')
  assert.equal(all.split('LLM Running (Step 2)').length - 1, 1)
})

function fenceBalance(text: string): number {
  let open = 0
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim()
    if (open === 0) {
      const opener = trimmed.match(/^(`{3,})([^`]*)$/)
      if (opener) open = opener[1]!.length
      continue
    }
    const standalone = trimmed.match(/^(`{3,})\s*$/)
    if (standalone && standalone[1]!.length >= open) {
      open = 0
      continue
    }
    const inline = trimmed.match(/`{3,}$/)
    if (inline && inline[0]!.length >= open) open = 0
  }
  return open
}

test('commitStreamingAssistantMessages does not split before a closer after an inline status fence', () => {
  // Real 2026-10-12 regression: the [Workflow] line ended with its closing
  // 5-backtick run on the same line. The old fence parser toggled on every
  // fence line, so it thought the next ````text opener had closed the outer
  // fence and could cut immediately before the [Info] closer. That closer then
  // became an opener in the live segment and swallowed Turn 4.
  const lines = [
    '`````',
    '[Workflow] launched model-availability-research (wf_test); use /workflows to monitor.`````',
    '',
    '**LLM Running (Step 3) ...**',
    '🔨 Tool: `update_working_checkpoint`  📥 args:',
    '````text',
    '{',
    '  "key_info": "x"',
    '}',
    '````',
    '`````',
    '[Info] Updated key_info and related_sop.',
    '`````',
    '',
    '**LLM Running (Step 4) ...**',
  ]
  const messages: ChatMessage[] = [liveAssistant(12, lines.join('\n'))]
  const next = commitStreamingAssistantMessages(messages, 12, 4)
  const live = next.find(m => m.id === 'a-12')
  assert.ok(live)
  const rendered = renderMarkdownLines(formatAssistantText(live!.text, { expanded: true }))
  const turn4 = rendered.find(line => line.text.includes('LLM Running (Step 4)'))
  assert.ok(turn4)
  assert.equal(turn4!.text, 'LLM Running (Step 4) ...')
  assert.equal(turn4!.parts?.some(part => part.bold), true)
})

test('commitStreamingAssistantMessages keeps committing while a tool-args block is still open', () => {
  // Regression (2026-10-03): requiring the live tail to be fence-balanced as well as the
  // head stalled every commit for the whole duration of each tool-args block. Live text
  // then grew far past the ~12-row live viewport, so a freshly emitted
  // '[Action] Spawning subagent: X' / '[Status] ...' pair was covered by its own args
  // payload before it could reach Static — "the third subagent never showed up".
  const lines: string[] = [
    '`````',
    '🔨 Tool: `spawn_agent`  📥 args:',
    '````text',
    '{',
    ...Array.from({ length: 40 }, (_, i) => `  "field_${i}": "value_${i}",`),
    '}',
    '````',
    '[Action] Spawning subagent: beta',
    '[Status] Subagent /root/beta started (pid=2).',
    '`````',
    '`````',
    '🔨 Tool: `spawn_agent`  📥 args:',
    '````text',
    '{',
    ...Array.from({ length: 10 }, (_, i) => `  "still_streaming_${i}": "v",`),
  ]
  const overflow = findFenceSafeOverflowCount(lines, 8)
  assert.ok(overflow > 0, 'an open trailing args block must not stall commits')
  assert.equal(fenceBalance(lines.slice(0, overflow).join('\n')), 0, 'committed head must end outside a fence')

  const messages: ChatMessage[] = [liveAssistant(11, lines.join('\n'))]
  const next = commitStreamingAssistantMessages(messages, 11, 8)
  const commits = next.filter(m => m.id.startsWith('a-11-c'))
  assert.ok(commits.length >= 1)
  assert.ok(commits.some(m => m.text.includes('[Action] Spawning subagent: beta')))
  const live = next.find(m => m.id === 'a-11')
  assert.ok((live?.text.split('\n').length ?? 99) < lines.length, 'live tail must shrink below the in-flight text')
  for (const segment of [...commits]) {
    assert.equal(fenceBalance(segment.text), 0, `commit ${segment.id} left an open fence:\n${segment.text}`)
  }
})

test('findFenceSafeOverflowCount picks a balanced head instead of stalling behind a trailing fence', () => {
  // Bare fence lines only, so the module's fence classifier and this file's fenceBalance agree.
  const lines = [
    'before',
    '`````',
    'inside_a_code_block',
    '`````',
    'after',
    '`````',
    'open_never_closed',
  ]
  const overflow = findFenceSafeOverflowCount(lines, 2)
  assert.ok(overflow > 0, 'a balanced head must be found instead of stalling')
  assert.equal(fenceBalance(lines.slice(0, overflow).join('\n')), 0)
})

test('commitStreamingAssistantMessages commits overflow lines and keeps a short live tail', () => {
  const lines = Array.from({ length: 12 }, (_, i) => `L${i}`).join('\n')
  const messages: ChatMessage[] = [liveAssistant(2, lines)]
  const next = commitStreamingAssistantMessages(messages, 2, 4)

  const commits = next.filter(m => m.id.startsWith('a-2-c'))
  const live = next.find(m => m.id === 'a-2')
  assert.equal(commits.length, 1)
  assert.equal(commits[0]?.done, true)
  assert.equal(commits[0]?.text, Array.from({ length: 8 }, (_, i) => `L${i}`).join('\n'))
  assert.equal(live?.done, false)
  assert.equal(live?.text, 'L8\nL9\nL10\nL11')
  assert.equal(live?.text.split('\n').length, 4)
})

test('commitStreamingAssistantMessages can commit multiple times as stream grows', () => {
  let messages: ChatMessage[] = [liveAssistant(3, Array.from({ length: 10 }, (_, i) => `A${i}`).join('\n'))]
  messages = commitStreamingAssistantMessages(messages, 3, 4)
  assert.equal(messages.filter(m => m.id.startsWith('a-3-c')).length, 1)

  const live = messages.find(m => m.id === 'a-3')!
  messages = messages.map(m => m.id === 'a-3' ? { ...m, text: `${live.text}\n${Array.from({ length: 6 }, (_, i) => `B${i}`).join('\n')}` } : m)
  messages = commitStreamingAssistantMessages(messages, 3, 4)

  const commits = messages.filter(m => m.id.startsWith('a-3-c'))
  const tail = messages.find(m => m.id === 'a-3')
  assert.ok(commits.length >= 2)
  assert.equal(tail?.text.split('\n').length, 4)
  assert.ok(commits.every(m => m.done))
  assert.equal(tail?.done, false)
})

test('commitStreamingAssistantMessages ignores unrelated tasks', () => {
  const messages: ChatMessage[] = [
    liveAssistant(1, Array.from({ length: 20 }, (_, i) => `X${i}`).join('\n')),
    liveAssistant(2, Array.from({ length: 20 }, (_, i) => `Y${i}`).join('\n')),
  ]
  const next = commitStreamingAssistantMessages(messages, 1, 4)
  assert.ok(next.some(m => m.id.startsWith('a-1-c')))
  assert.equal(next.filter(m => m.id.startsWith('a-2-c')).length, 0)
  assert.equal(next.find(m => m.id === 'a-2')?.text.split('\n').length, 20)
})

test('committedAssistantPrefix joins commit segments in order', () => {
  const messages: ChatMessage[] = [
    { id: 'a-1-c0', role: 'assistant', text: 'A0\nA1', done: true, taskId: 1 },
    { id: 'a-1-c1', role: 'assistant', text: 'A2\nA3', done: true, taskId: 1 },
    liveAssistant(1, 'A4\nA5'),
  ]
  assert.equal(committedAssistantPrefix(messages, 1), 'A0\nA1\nA2\nA3')
  assert.equal(committedAssistantPrefix(messages, 9), '')
})

test('remainingAssistantTextAfterCommits drops already-committed prefix from full final text', () => {
  const committed = 'L0\nL1\nL2'
  const full = 'L0\nL1\nL2\nL3\nL4'
  assert.equal(remainingAssistantTextAfterCommits(full, committed), 'L3\nL4')
  assert.equal(remainingAssistantTextAfterCommits(full, ''), full)
  assert.equal(remainingAssistantTextAfterCommits('only-live', 'other'), 'only-live')
})

test('remainingAssistantTextAfterCommits handles exact full commit (empty remainder)', () => {
  const body = 'A\nB\nC'
  assert.equal(remainingAssistantTextAfterCommits(body, body), '')
})

test('remainingAssistantTextAfterCommits tolerates summary newline injection on done', () => {
  // agentmain rewrites "</summary>" → "</summary>\n\n" only on the done payload.
  // Streamed deltas still have a single newline, so exact-prefix strip used to fail and
  // the full turn (including LLM Running Step 1) was appended again on top of commits.
  const streamed = [
    '**LLM Running (Step 1) ...**',
    '',
    '<summary>需要当前时间，读取系统钟</summary>',
    'tool output',
    '**LLM Running (Step 2) ...**',
    '',
    '现在是 **2026年7月14日 15:28**。',
  ].join('\n')
  const done = streamed.replace('</summary>', '</summary>\n\n')
  const commits = streamed.split('\n').slice(0, 4).join('\n')
  const remaining = remainingAssistantTextAfterCommits(done, commits)
  assert.equal(remaining.includes('LLM Running (Step 1)'), false)
  assert.match(remaining, /LLM Running \(Step 2\)/)
  assert.match(remaining, /15:28/)
})

test('remainingAssistantTextAfterCommits strips when done equals commits+live under newline flex', () => {
  const commits = 'A\n<summary>x</summary>\nB'
  const live = 'C\nD'
  const streamed = `${commits}\n${live}`
  const done = 'A\n<summary>x</summary>\n\nB\nC\nD'
  // Prefer: remaining after commits is the live tail (plus any extra blank from summary rewrite).
  const rest = remainingAssistantTextAfterCommits(done, commits)
  assert.equal(rest.includes('A\n'), false)
  assert.match(rest, /C/)
  assert.match(rest, /D/)
  assert.equal(remainingAssistantTextAfterCommits(done, streamed).trim(), '')
})

test('DEFAULT_STREAM_LIVE_TAIL_LINES is a positive stable cap', () => {
  assert.ok(DEFAULT_STREAM_LIVE_TAIL_LINES >= 4)
  assert.ok(DEFAULT_STREAM_LIVE_TAIL_LINES <= 12)
})

test('end-to-end applyBridgeEvent long stream: single-channel + no duplicate lines', () => {
  let state = applyBridgeEvent(initialState, { type: 'ready', version: 1 })
  state = applyBridgeEvent(state, { type: 'user', taskId: 9, text: 'long-q' })
  state = applyBridgeEvent(state, { type: 'status', status: 'running', taskId: 9 })

  for (let b = 0; b < 5; b++) {
    const chunk = Array.from({ length: 6 }, (_, i) => `T${b}-${i}`).join('\n') + '\n'
    state = applyBridgeEvent(state, { type: 'assistant_delta', taskId: 9, text: chunk })
    const split = splitStaticAndActiveMessages(state.messages, { keepLatestTaskActive: true })
    // Once assistant streams, user is finalized into Static (append-only chronological order).
    assert.ok(split.staticMessages.some(m => m.id === 'u-9'))
    assert.equal(split.activeMessages.some(m => m.id === 'u-9'), false)
    // live assistant short
    const liveA = split.activeMessages.find(m => m.id === 'a-9')
    if (liveA) {
      assert.ok(liveA.text.split('\n').length <= DEFAULT_STREAM_LIVE_TAIL_LINES)
    }
    // commits only static
    for (const c of split.staticMessages.filter(m => m.id.startsWith('a-9-c'))) {
      assert.equal(c.done, true)
      assert.equal(split.activeMessages.some(m => m.id === c.id), false)
    }
    // user always before assistant commits in static list
    const staticIds = split.staticMessages.map(m => m.id)
    assert.ok(staticIds.indexOf('u-9') < staticIds.findIndex(id => id.startsWith('a-9-c')) || !staticIds.some(id => id.startsWith('a-9-c')))
  }

  const full = Array.from({ length: 30 }, (_, i) => {
    const b = Math.floor(i / 6)
    const j = i % 6
    return `T${b}-${j}`
  }).join('\n') + '\n'

  state = applyBridgeEvent(state, { type: 'assistant_done', taskId: 9, text: full })
  state = applyBridgeEvent(state, { type: 'status', status: 'idle' })

  const end = splitStaticAndActiveMessages(state.messages)
  assert.equal(end.activeMessages.length, 0)
  assert.ok(end.staticMessages.some(m => m.id === 'u-9' && m.done))

  const allAssistant = end.staticMessages.filter(m => m.role === 'assistant').map(m => m.text).join('\n')
  for (const line of full.trim().split('\n')) {
    assert.equal(
      allAssistant.split('\n').filter(l => l === line).length,
      1,
      `duplicated line ${line}`,
    )
  }
})

test('findFenceSafeOverflowCount never cuts immediately after a tool header', () => {
  const lines = [
    'before',
    '⏺ Tool: `code_run` args:',
    '````text',
    '{',
    ...Array.from({ length: 40 }, (_, index) => `  "line_${index}": ${index},`),
  ]
  const headerIndex = lines.indexOf('⏺ Tool: `code_run` args:')
  const overflow = findFenceSafeOverflowCount(lines, 8)
  assert.ok(overflow > 0)
  assert.ok(overflow <= headerIndex, `cut ${overflow} must not include the tool header at ${headerIndex}`)
  assert.equal(lines.slice(0, overflow).at(-1), 'before')
})

test('commitStreamingAssistantMessages keeps a tool header and args fence in the same segment', () => {
  const lines = [
    'intro',
    '⏺ Tool: `code_run` args:',
    '````text',
    '{',
    ...Array.from({ length: 30 }, (_, index) => `  "line_${index}": ${index},`),
  ]
  const messages: ChatMessage[] = [liveAssistant(21, lines.join('\n'))]
  const next = commitStreamingAssistantMessages(messages, 21, 8)
  const commits = next.filter(message => message.id.startsWith('a-21-c'))
  assert.ok(commits.length >= 1)
  for (const commit of commits) {
    assert.doesNotMatch(commit.text, /Tool:\s*`[^`]+`.*args:\s*$/m)
  }
  const live = next.find(message => message.id === 'a-21')
  assert.match(live?.text ?? '', /Tool:\s*`code_run` args:/)
  assert.match(live?.text ?? '', /^````text/m)
})

test('findFenceSafeOverflowCount never cuts immediately after a [Stdout] marker', () => {
  const lines = [
    'intro',
    ...Array.from({ length: 20 }, (_, index) => `prelude ${index}`),
    '[Stdout]',
    ...Array.from({ length: 8 }, (_, index) => `stdout line ${index}`),
  ]
  const markerIndex = lines.indexOf('[Stdout]')
  const overflow = findFenceSafeOverflowCount(lines, 8)

  assert.ok(overflow > 0)
  assert.ok(overflow <= markerIndex, `cut ${overflow} must not separate [Stdout] from its body`)
  assert.notEqual(lines.slice(0, overflow).at(-1), '[Stdout]')
})
