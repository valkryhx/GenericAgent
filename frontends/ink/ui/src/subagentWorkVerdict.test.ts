/**
 * The work verdict must come from the work status, never from the process status.
 *
 * This is the second time this class of bug has been reported, so the tests pin the
 * *rule* rather than one instance of it. The first fix special-cased `killed`; that was
 * too narrow. Real captured bridge frames (`temp/real-subagent-fold-e2e/*`) show the
 * record for a child that finished its work looking like this:
 *
 *     status         = 'succeeded'     (projected; the bridge got this right)
 *     sourceStatus   = 'waiting_reply' (the PROCESS status, not the work verdict)
 *     turnStatus     = 'completed'
 *     metadata.completionStatus = None
 *
 * `sourceStatus` carries `process_status` for process records (`_record_from_state`:
 * `source_status=state.process_status`), so it is `exited` / `shutdown` / `waiting_reply`
 * / `killed` -- a vocabulary that never contains `completed`. Treating it as the work
 * verdict marks every finished child as failed.
 *
 * Note the mismatch that makes this easy to get wrong: for *workflow* records
 * `source_status` is the run status (`workflow.py:263`), which really is a work verdict.
 * One field, two meanings. So the UI cannot trust it for either engine; it must ask the
 * fields that are unambiguous (`metadata.completionStatus`, `turnStatus`).
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { agentNoticeFromRecord, type AgentRecordLike } from './agentNotice.js'

/** A real record, copied verbatim from a captured bridge stream. */
function realCompletedChild(overrides: Partial<AgentRecordLike> = {}): AgentRecordLike {
  return {
    executionId: 'process-agent:run_000001:%2Froot%2Fprobe_fast',
    status: 'succeeded',
    sourceStatus: 'waiting_reply',
    agentPath: '/root/probe_fast',
    metadata: { turnStatus: 'completed' },
    ...overrides,
  }
}

test('a child whose process is still waiting for a reply is announced as completed', () => {
  // The regression: `waiting_reply` is not in any success vocabulary, so a finished child
  // was announced in red on every snapshot.
  const notice = agentNoticeFromRecord(realCompletedChild())
  assert.ok(notice, 'a terminal child produced no announcement at all')
  assert.equal(notice.ok, true, `announced as ${notice.word}, but the work completed`)
  assert.equal(notice.word, 'completed')
})

test('every process status that is not a work verdict leaves the work verdict alone', () => {
  // These are all values `process_status` really takes (`manager.py`, `close_agent`).
  // None of them says anything about whether the child's work succeeded.
  for (const processStatus of ['waiting_reply', 'exited', 'shutdown', 'alive', 'killed']) {
    const notice = agentNoticeFromRecord(realCompletedChild({
      sourceStatus: processStatus,
      metadata: { turnStatus: 'completed' },
    }))
    assert.ok(notice, `${processStatus}: no announcement`)
    assert.equal(notice.ok, true, `${processStatus}: a completed child was announced as ${notice.word}`)
  }
})

test('metadata.completionStatus is the work verdict when the bridge provides it', () => {
  const notice = agentNoticeFromRecord(realCompletedChild({
    sourceStatus: 'exited',
    metadata: { completionStatus: 'completed' },
  }))
  assert.ok(notice)
  assert.equal(notice.ok, true)
  assert.equal(notice.word, 'completed')
})

test('a child that really failed is still announced as failed', () => {
  // The failure path must survive: this is why the fix cannot just say "trust `status`".
  const notice = agentNoticeFromRecord(realCompletedChild({
    status: 'failed',
    sourceStatus: 'exited',
    metadata: { completionStatus: 'failed', turnStatus: 'errored' },
  }))
  assert.ok(notice)
  assert.equal(notice.ok, false)
  assert.equal(notice.word, 'failed')
})

test('a child that errored with no completionStatus is still a failure', () => {
  const notice = agentNoticeFromRecord(realCompletedChild({
    status: 'failed',
    sourceStatus: 'exited',
    metadata: { turnStatus: 'errored' },
  }))
  assert.ok(notice)
  assert.equal(notice.ok, false)
})

test('a child cut off mid-work is not announced as a success', () => {
  // No work verdict and the process was force-terminated: the child really was cut off.
  const notice = agentNoticeFromRecord(realCompletedChild({
    status: 'killed',
    sourceStatus: 'killed',
    metadata: {},
  }))
  assert.ok(notice)
  assert.equal(notice.ok, false)
})

test('a process lifecycle word is never read as the work verdict, even a positive-sounding one', () => {
  // The distinguishing case. `waiting_reply` is not in the success vocabulary, so a naive
  // implementation also happens to answer "failed" -- the right answer for the wrong
  // reason. This test pins the actual rule by asking for the *name* the notice reports:
  // the verdict must come from turnStatus (`completed`), not from the sourceStatus word.
  //
  // If the lifecycle filter is removed AND someone later adds a lifecycle word to the
  // success set, this is the test that notices, because the reported word changes.
  const notice = agentNoticeFromRecord(realCompletedChild({
    sourceStatus: 'waiting_reply',
    metadata: { turnStatus: 'completed' },
  }))
  assert.ok(notice)
  assert.equal(notice.word, 'completed', 'the verdict came from sourceStatus, not from the work status')
})

test('a real failure verdict still beats a success it would otherwise inherit', () => {
  // This is why the projected `status` cannot simply be trusted alone: a record can carry
  // an explicit failing work verdict while the projection says otherwise. A failure is
  // never overridden upward.
  const notice = agentNoticeFromRecord(realCompletedChild({
    status: 'succeeded',
    sourceStatus: 'exited',
    metadata: { completionStatus: 'failed', turnStatus: 'errored' },
  }))
  assert.ok(notice)
  assert.equal(notice.ok, false)
  assert.equal(notice.word, 'failed')
})

test('the projected status is not overridden by a stale success in metadata', () => {
  // Symmetry: a work verdict that says failed must not be rescued by `status`.
  const notice = agentNoticeFromRecord(realCompletedChild({
    status: 'succeeded',
    sourceStatus: 'exited',
    metadata: { turnStatus: 'errored' },
  }))
  assert.ok(notice)
  assert.equal(notice.ok, false)
})

test('a real failure verdict still loses to a success it would otherwise inherit', () => {
  // This is the guard the mutation check kept catching: an explicit failing work verdict
  // must never be rescued by a projected success. Covered on both engines.
  for (const record of [
    realCompletedChild({ status: 'succeeded', sourceStatus: 'exited', metadata: { completionStatus: 'failed' } }),
    { executionId: 'workflow-run:run_x', status: 'succeeded', sourceStatus: 'failed', metadata: {} },
  ]) {
    const notice = agentNoticeFromRecord(record as AgentRecordLike)
    assert.ok(notice)
    assert.equal(notice.ok, false, `${(record as AgentRecordLike).executionId}: a failure was announced as a success`)
  }
})

test('with no metadata verdict at all, a lifecycle sourceStatus must not decide the outcome', () => {
  // THE dangerous combination, and the one no other test covered: the bridge sends no
  // `metadata.completionStatus` and no `turnStatus`, so the verdict chain falls through to
  // `sourceStatus`. If the lifecycle filter is missing, `waiting_reply` becomes the verdict
  // -- and although it is not a success word, treating a *process* word as a *work* word is
  // the exact mistake this whole fix exists to stop. The projected `status` is the only
  // thing here that actually knows the work finished.
  const notice = agentNoticeFromRecord({
    executionId: 'process-agent:run_000001:%2Froot%2Fprobe_slow',
    status: 'succeeded',
    sourceStatus: 'waiting_reply',
    agentPath: '/root/probe_slow',
    metadata: {},
  })
  assert.ok(notice, 'no announcement at all')
  assert.equal(notice.ok, true, 'the projected work status was discarded in favour of a process word')

  // Same shape, but the process was reclaimed after finishing: still a success.
  for (const processStatus of ['shutdown', 'exited', 'killed', 'alive']) {
    const reclaimed = agentNoticeFromRecord({
      executionId: `process-agent:run_1:%2Froot%2Fx_${processStatus}`,
      status: 'succeeded',
      sourceStatus: processStatus,
      metadata: {},
    })
    assert.ok(reclaimed, `${processStatus}: no announcement`)
    assert.equal(reclaimed.ok, true, `${processStatus}: a finished child was announced as ${reclaimed.word}`)
  }
})

test('a workflow record keeps working: there sourceStatus really is the run status', () => {
  // `workflow.py:263` puts the run status in `source_status`, so `succeeded` there is a
  // work verdict. The fix must not break this engine while fixing the other one.
  const notice = agentNoticeFromRecord({
    executionId: 'workflow-run:run_abc',
    status: 'succeeded',
    sourceStatus: 'succeeded',
    metadata: { taskName: 'wf' },
  })
  assert.ok(notice)
  assert.equal(notice.ok, true)
})

test('a workflow record that failed is not announced as a success', () => {
  const notice = agentNoticeFromRecord({
    executionId: 'workflow-run:run_abc',
    status: 'failed',
    sourceStatus: 'failed',
    metadata: { taskName: 'wf' },
  })
  assert.ok(notice)
  assert.equal(notice.ok, false)
})
