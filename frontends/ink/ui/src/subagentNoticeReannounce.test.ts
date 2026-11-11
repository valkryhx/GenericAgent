/**
 * A subagent announcement must not come back after the turn that produced it ended.
 *
 * The reported symptom: while an **agent team** turn runs, the UI shows subagent lines
 * belonging to a **workflow run from an earlier turn**:
 *
 *     ✓ Subagent workflow-child:wf_6dcbb054f348476c848c68b2c6d85071:agent_1 completed
 *     ✓ Subagent workflow-run:wf_6dcbb054f348476c848c68b2c6d85071 completed
 *
 * Captured from a real session (`temp/ink_trace/frames-23960.log`): the lines first
 * appear at the moment the workflow finished (frame 357), are cleared when that turn
 * ends (frame 360), and then reappear at frame 537 when the *next* turn -- an agent
 * team run -- starts, staying on screen for minutes.
 *
 * Why they come back: `_emit_agent_snapshot` sends `list_records(include_terminal=True)`,
 * i.e. every terminal record in the session's workflow store, on every snapshot. The
 * announcement merge seeded its `byExecution` map *from the currently displayed
 * notices*. Turn end clears those notices, so on the next snapshot the map is empty and
 * every already-announced record looks brand new.
 *
 * The rule this file pins: **an execution id is announced at most once**; clearing the
 * visual line does not make it announceable again. Verdict *refinement* within a turn
 * (a child that reports `closed` and then `succeeded`) must still update in place -- that
 * is the behaviour `agentNotice.test.ts` already relies on.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { mergeAgentNotices, type AgentNotice, type AgentRecordLike } from './agentNotice.js'

/** The real workflow record, reproduced against the live store. */
function workflowChildRecord(jobId = 'agent_1'): AgentRecordLike {
  return {
    executionId: `workflow-child:wf_6dcbb054f348476c848c68b2c6d85071:${jobId}`,
    status: 'succeeded',
    sourceStatus: 'succeeded',
    metadata: { label: 'commit-HEAD', callIndex: 0 },
  }
}

function workflowRunRecord(): AgentRecordLike {
  return {
    executionId: 'workflow-run:wf_6dcbb054f348476c848c68b2c6d85071',
    status: 'succeeded',
    sourceStatus: 'succeeded',
    metadata: {},
  }
}

function noticeFor(executionId: string, word = 'completed'): AgentNotice {
  return { executionId, name: executionId, ok: word === 'completed', word }
}

test('a record announced in an earlier turn is not announced again after a clear', () => {
  const records = [workflowChildRecord(), workflowRunRecord()]

  // Turn 1: the workflow finishes while running -> announced.
  const first = mergeAgentNotices([], records, new Set())
  assert.equal(first.notices.length, 2, `expected 2 announcements, got ${first.notices.length}`)
  assert.equal(first.changed, true)

  // Turn 1 ends -> the view clears the notices but remembers what it announced.
  const cleared: AgentNotice[] = []

  // Turn 2 (agent team): the same terminal records are still in the snapshot.
  const second = mergeAgentNotices(cleared, records, first.announced)
  assert.deepEqual(
    second.notices,
    [],
    `stale workflow announcements came back: ${JSON.stringify(second.notices.map(n => n.executionId))}`,
  )
  assert.equal(second.changed, false, 'a no-op merge must not rewrite the chrome')
})

test('a genuinely new child is still announced after an older one was cleared', () => {
  const first = mergeAgentNotices([], [workflowChildRecord('agent_1')], new Set())
  const cleared: AgentNotice[] = []

  const second = mergeAgentNotices(cleared, [workflowChildRecord('agent_2')], first.announced)
  assert.equal(second.notices.length, 1)
  assert.equal(second.notices[0].executionId, 'workflow-child:wf_6dcbb054f348476c848c68b2c6d85071:agent_2')
})

test('a verdict moving within the same turn still updates in place', () => {
  // The pre-existing behaviour: a child goes `closed` (no verdict, silent) then reports
  // `succeeded`. Once shown, a later refinement must reach the screen.
  const running: AgentRecordLike = {
    executionId: 'process-agent:run_1:%2Froot%2Fsub1',
    status: 'closed',
    sourceStatus: 'exited',
    metadata: { completionStatus: 'pending', turnStatus: 'running' },
  }
  const done: AgentRecordLike = {
    ...running,
    status: 'succeeded',
    sourceStatus: 'waiting_reply',
    metadata: { completionStatus: 'completed', turnStatus: 'completed' },
  }

  const step1 = mergeAgentNotices([], [running], new Set())
  assert.deepEqual(step1.notices, [], 'a child with no verdict must stay silent')

  const step2 = mergeAgentNotices(step1.notices, [done], step1.announced)
  assert.equal(step2.notices.length, 1)
  assert.equal(step2.notices[0].ok, true)
})

test('the newest announcements win and the list stays bounded', () => {
  const records = Array.from({ length: 10 }, (_, index) => workflowChildRecord(`agent_${index}`))
  const merged = mergeAgentNotices([], records, new Set())
  assert.equal(merged.notices.length, 6, 'the chrome must not grow without bound')
  assert.equal(merged.notices.at(-1)?.executionId, 'workflow-child:wf_6dcbb054f348476c848c68b2c6d85071:agent_9')
})

test('every announced id is recorded, including ones trimmed off the display', () => {
  // Otherwise a trimmed-but-announced id would re-announce the moment the list has room.
  const records = Array.from({ length: 10 }, (_, index) => workflowChildRecord(`agent_${index}`))
  const merged = mergeAgentNotices([], records, new Set())
  assert.equal(merged.announced.size, 10)
})