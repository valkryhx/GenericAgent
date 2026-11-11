/**
 * A child that has not produced a work verdict yet is not a failure.
 *
 * The reported symptom: in `ga`, a subagent occasionally flashes a red
 * `✗ Subagent <name> failed` the instant it starts.
 *
 * The captured record, reproduced against the real projection
 * (`ProcessSubagentAdapter._project_status` + `_refresh_state`):
 *
 *     status         = 'closed'      (projected: no work verdict, process gone)
 *     sourceStatus   = 'exited'      (the PROCESS status)
 *     metadata.completionStatus = 'pending'
 *     metadata.turnStatus       = 'running'
 *
 * `_refresh_state` rewrites a `pending` turn to `running` when the child is not
 * alive, and `_project_status` then projects the record to `closed` because
 * `process_status == 'exited'`. `completion_status` was never assigned a work
 * verdict, so it stays `pending`.
 *
 * `agentNoticeFromRecord` used to treat that chain as "not a success word, so
 * it failed", and announced `✗ failed`. The verdict chain is a *positive* check
 * (`COMPLETED_STATUSES`), so every word it does not recognise becomes a
 * failure -- which is right for `errored`/`killed` and wrong for `pending`,
 * which is not a verdict at all.
 *
 * The rule this file pins: **a record with no work verdict is not announced.**
 * Silence and a later real verdict beat a confident lie. A genuinely failed
 * child still announces `failed` -- that is covered in
 * `subagentWorkVerdict.test.ts` and re-asserted here so the fix cannot be
 * "announce nothing ever".
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import { agentNoticeFromRecord, type AgentRecordLike } from './agentNotice.js'

/** Reproduced from the real manager + adapter, not hand-invented. */
function spawnVanishedChild(overrides: Partial<AgentRecordLike> = {}): AgentRecordLike {
  return {
    executionId: 'process-agent:run_flicker:%2Froot%2Fsub1',
    status: 'closed',
    sourceStatus: 'exited',
    agentPath: '/root/sub1',
    metadata: { completionStatus: 'pending', turnStatus: 'running' },
    ...overrides,
  }
}

test('a child with no work verdict yet is not announced as failed', () => {
  const notice = agentNoticeFromRecord(spawnVanishedChild())
  // Silence is the fix: there is no verdict to report, so nothing is announced.
  assert.equal(
    notice,
    null,
    notice ? `announced as ${notice.word} before the child had a work verdict` : '',
  )
})

test('an empty completionStatus with a running turn is not announced', () => {
  // `completion_status` is omitted from metadata when the state never got one
  // (`_metadata_from_state` drops None), so the chain falls through to turnStatus.
  const notice = agentNoticeFromRecord(spawnVanishedChild({
    metadata: { turnStatus: 'running' },
  }))
  assert.equal(notice, null, notice ? `announced as ${notice.word} from a running turn` : '')
})

test('a pending turn with no completionStatus at all is not announced', () => {
  const notice = agentNoticeFromRecord(spawnVanishedChild({
    metadata: { turnStatus: 'pending' },
  }))
  assert.equal(notice, null, notice ? `announced as ${notice.word} from a pending turn` : '')
})

test('a pending completionStatus is not announced even when the projection says succeeded', () => {
  // Guard the other direction: `pending` must not be silently promoted to a success
  // either. It is "no verdict", not "probably fine".
  const notice = agentNoticeFromRecord(spawnVanishedChild({
    status: 'succeeded',
    metadata: { completionStatus: 'pending' },
  }))
  assert.equal(notice, null, notice ? `announced as ${notice.word} with completionStatus=pending` : '')
})

test('a real failure verdict is still announced as failed', () => {
  // The fix must not turn the notice into a mute switch.
  const notice = agentNoticeFromRecord(spawnVanishedChild({
    status: 'failed',
    metadata: { completionStatus: 'failed', turnStatus: 'errored' },
  }))
  assert.ok(notice, 'a genuinely failed child produced no announcement')
  assert.equal(notice.ok, false)
  assert.equal(notice.word, 'failed')
})

test('a startup failure that never got a turn is still announced as failed', () => {
  // The startup-handshake path writes turn_status=errored + completion_status=failed
  // (`_record_startup_failure`), so this is a real verdict and must still be red.
  const notice = agentNoticeFromRecord(spawnVanishedChild({
    status: 'failed',
    metadata: { completionStatus: 'failed', turnStatus: 'errored' },
  }))
  assert.ok(notice)
  assert.equal(notice.ok, false)
})

test('a completed child is still announced as completed', () => {
  const notice = agentNoticeFromRecord(spawnVanishedChild({
    status: 'succeeded',
    sourceStatus: 'waiting_reply',
    metadata: { completionStatus: 'completed', turnStatus: 'completed' },
  }))
  assert.ok(notice)
  assert.equal(notice.ok, true)
  assert.equal(notice.word, 'completed')
})

test('a startup-exited child with no verdict stays silent rather than flashing red', () => {
  // The exact reported shape: startup_phase=startup_exited, process gone before the
  // child ever ran. `_record_startup_failure` DOES write a verdict (errored/failed),
  // so this case is red on purpose -- but the *pre-verdict* window must not be.
  const beforeVerdict = agentNoticeFromRecord(spawnVanishedChild({
    metadata: { completionStatus: 'pending', turnStatus: 'running' },
  }))
  assert.equal(beforeVerdict, null)

  const afterVerdict = agentNoticeFromRecord(spawnVanishedChild({
    status: 'failed',
    metadata: { completionStatus: 'failed', turnStatus: 'errored' },
  }))
  assert.ok(afterVerdict)
  assert.equal(afterVerdict.ok, false)
})