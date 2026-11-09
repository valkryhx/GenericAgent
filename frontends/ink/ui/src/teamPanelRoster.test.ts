/**
 * The team panel must still show the team after the team has finished.
 *
 * Measured on real data before the fix: /team held 0 members for a finished session,
 * because `manager.close_agent` marks every child `status=closed` and the roster reader
 * filtered closed rows out. The panel's whole purpose is answering "how did my team do",
 * and it was blank exactly then.
 *
 * Two separate requirements:
 *   1. finished members stay listed
 *   2. a finished member says how it ended (`completed`, not just `closed`)
 *
 * Requirement 2 is the same lesson as `agentNotice.ts`: a lifecycle word (`closed`) is not
 * a work verdict. The registry keeps the verdict in `previousStatus`, so the panel has to
 * read it rather than trusting `status`.
 */
import test from 'node:test'
import assert from 'node:assert/strict'
import {
  teamMemberGlyph,
  teamMemberVerdict,
  teamPanelLines,
  type TeamPanelState,
} from './teamPanel.js'
import type { TeamMember } from './protocol.js'

function member(overrides: Partial<TeamMember> = {}): TeamMember {
  return {
    agentId: '/root/probe_fast',
    name: 'probe_fast',
    status: 'running',
    parentPath: '/root',
    runId: 'run_000001',
    ...overrides,
  }
}

function state(members: TeamMember[]): TeamPanelState {
  return {
    roster: { teamName: 'session-test', leadAgentId: '/root', leadName: 'team-lead', memberCount: members.length },
    members,
    tasks: [],
    selected: 0,
  }
}

test('a finished member still gets its own row', () => {
  const lines = teamPanelLines(state([
    member({ name: 'alpha' }),
    member({ name: 'bravo', status: 'closed', previousStatus: 'completed', closedStatus: 'shutdown' }),
    member({ name: 'charlie', status: 'closed', previousStatus: 'errored', closedStatus: 'killed' }),
  ]))
  const text = lines.join('\n')
  for (const name of ['alpha', 'bravo', 'charlie']) {
    assert.ok(text.includes(name), `${name} vanished from the panel:\n${text}`)
  }
})

test('a finished member reports how it ended, not just that its row is closed', () => {
  // The bug this pins: a member whose work completed and a member that errored both hold
  // `status === 'closed'`, so the panel used to render them identically.
  const completed = teamMemberVerdict({ status: 'closed', previousStatus: 'completed', closedStatus: 'shutdown' })
  const errored = teamMemberVerdict({ status: 'closed', previousStatus: 'errored', closedStatus: 'killed' })
  assert.equal(completed, 'completed')
  assert.equal(errored, 'errored')
  assert.notEqual(completed, errored, 'finished and failed members render identically')
})

test('a live member keeps its lifecycle word as the verdict', () => {
  assert.equal(teamMemberVerdict({ status: 'running' }), 'running')
  assert.equal(teamMemberVerdict({ status: 'idle' }), 'idle')
})

test('a live member whose turn already ended reports the turn verdict, not "running"', () => {
  // A background teammate stays alive between turns: `status="running"` with
  // `turn_status="completed"`. Real data: 24 of 80 registry rows sat exactly there, so a
  // completed member painted as `● running` -- the same lifecycle-word trap as `closed`.
  assert.equal(teamMemberVerdict({ status: 'running', turnStatus: 'completed' }), 'completed')
  assert.equal(teamMemberGlyph({ status: 'running', turnStatus: 'completed' }), '✓')
  assert.equal(teamMemberVerdict({ status: 'running', turnStatus: 'errored' }), 'errored')
  assert.equal(teamMemberGlyph({ status: 'running', turnStatus: 'errored' }), '✗')
})

test('a live member still working keeps running, and a mid-turn value is not a verdict', () => {
  assert.equal(teamMemberVerdict({ status: 'running', turnStatus: 'running' }), 'running')
  assert.equal(teamMemberVerdict({ status: 'running', turnStatus: 'pending' }), 'running')
  assert.equal(teamMemberVerdict({ status: 'running', turnStatus: null }), 'running')
})

test('the glyph distinguishes a completed member from an idle one', () => {
  // A hollow circle reads as "not started". Showing that for a member that finished is
  // the worst possible answer: the user thinks it is stuck.
  const completed = teamMemberGlyph({ status: 'closed', previousStatus: 'completed', closedStatus: 'shutdown' })
  const idle = teamMemberGlyph({ status: 'idle' })
  assert.notEqual(completed, idle, 'a completed member looks idle')
  assert.equal(completed, '✓', 'a completed member should read as a check')
})

test('a member that errored gets the failure glyph, not the completed one', () => {
  assert.equal(teamMemberGlyph({ status: 'closed', previousStatus: 'errored', closedStatus: 'killed' }), '✗')
  assert.equal(teamMemberGlyph({ status: 'closed', previousStatus: 'interrupted', closedStatus: 'shutdown' }), '✗')
})

test('an unknown verdict is visible as unknown instead of silently becoming idle', () => {
  // The old fallback painted anything unrecognised as a hollow circle, which is
  // indistinguishable from idle. The panel must show that it does not know.
  assert.equal(teamMemberGlyph({ status: 'closed', previousStatus: 'some-new-state' }), '?')
})

test('the panel row shows the verdict text, so the glyph is not the only signal', () => {
  const lines = teamPanelLines(state([
    member({ status: 'closed', previousStatus: 'completed', closedStatus: 'shutdown' }),
  ]))
  const row = lines.find(line => line.includes('probe_fast'))
  assert.ok(row, `no row for the member:\n${lines.join('\n')}`)
  assert.match(row!, /completed/, `the row does not say the member completed: ${row}`)
})

test('a row reaped as stale reads as stale, not as running', () => {
  // `_reap_stale_agents` closes a dead row with `previous_status="running"` and
  // `closed_status="stale"`. "running" is a lifecycle word, not a verdict: without this
  // the row painted as `● running` even after the registry had reaped it.
  assert.equal(
    teamMemberVerdict({ status: 'closed', previousStatus: 'running', closedStatus: 'stale' }),
    'stale',
  )
  assert.equal(
    teamMemberGlyph({ status: 'closed', previousStatus: 'running', closedStatus: 'stale' }),
    '×',
  )
})

test('a released row with no turn verdict reads as released', () => {
  assert.equal(
    teamMemberVerdict({ status: 'closed', previousStatus: 'running', closedStatus: 'released' }),
    'released',
  )
})

test('a legacy closed row falls back to the turn verdict', () => {
  // `closed_status=released` writes `previous_status="running"`, a lifecycle word rather
  // than a verdict. The turn status is then the only surviving work verdict.
  assert.equal(
    teamMemberVerdict({ status: 'closed', previousStatus: 'running', closedStatus: 'released', turnStatus: 'completed' }),
    'completed',
  )
})

test('a running row the registry calls dead does not claim to be working', () => {
  // Real data: 29 of 29 `status=running` rows were dead by the registry's own probe --
  // the process was gone but nobody ever closed the row.
  assert.equal(teamMemberVerdict({ status: 'running', live: false }), 'stale')
  assert.equal(teamMemberGlyph({ status: 'running', live: false }), '×')
})

test('a finished turn outranks a dead process row', () => {
  assert.equal(
    teamMemberVerdict({ status: 'running', live: false, turnStatus: 'completed' }),
    'completed',
  )
})

test('unknown liveness is not treated as dead', () => {
  assert.equal(teamMemberVerdict({ status: 'running' }), 'running')
  assert.equal(teamMemberVerdict({ status: 'running', live: true }), 'running')
})

test('the panel row says stale, so the glyph is not the only signal', () => {
  const lines = teamPanelLines(state([member({ status: 'running', live: false })]))
  const row = lines.find(line => line.includes('probe_fast'))
  assert.ok(row, `no row for the member:\n${lines.join('\n')}`)
  assert.match(row!, /stale/)
})

test('a member closed without ever running does not claim it completed', () => {
  // `closed_status=worktree_error` happens before any turn. There is no work verdict, so
  // the panel must not invent one -- but it is a failure, not an unknown state either.
  const verdict = teamMemberVerdict({ status: 'closed', previousStatus: 'starting', closedStatus: 'worktree_error' })
  assert.notEqual(verdict, 'completed', 'a member that never ran was reported as completed')
  assert.equal(verdict, 'failed')
  assert.equal(teamMemberGlyph({ status: 'closed', previousStatus: 'starting', closedStatus: 'worktree_error' }), '✗')
})
