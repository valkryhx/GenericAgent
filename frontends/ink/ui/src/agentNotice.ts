/**
 * The subagent announcement line, as a function.
 *
 * Extracted from `App.tsx` so the *contract* -- green and bold for a completion, the
 * error colour for a failure -- can be asserted without depending on whether chalk
 * decided the test process has a colour terminal. chalk makes that decision once at
 * module load, so a test that sets `FORCE_COLOR` before rendering is already too late
 * and fails for reasons unrelated to the UI.
 */
import React from 'react'
import { Text } from 'ink'

export type AgentNotice = {
  executionId: string
  name: string
  ok: boolean
  word: string
  detail?: string
}

/**
 * Statuses that mean "this child is done and the user should be told".
 *
 * `killed` is here because a child can be reclaimed by force *after* finishing its work:
 * `close_agent` waits `grace_s` for the process to exit and then calls
 * `terminate_process()`, so a child that took slightly longer to exit ends up with
 * `process_status == "killed"` while `completion_status == "completed"` and its output is
 * on disk. How a process was reclaimed says nothing about whether its work succeeded.
 */
export const AGENT_TERMINAL_STATUSES = new Set([
  'succeeded',
  'completed',
  'failed',
  'errored',
  'error',
  'killed',
  'cancelled',
  'canceled',
  'closed',
  'degraded',
])

export type AgentRecordLike = {
  executionId?: string
  status?: string
  /** The pre-projection status; `completed` here beats a `killed` process status. */
  sourceStatus?: string | null
  agentPath?: string | null
  metadata?: Record<string, unknown>
}

/** Words that mean the *work* succeeded, whatever the process ended up doing. */
const COMPLETED_STATUSES = new Set(['succeeded', 'completed'])

/**
 * Process-lifecycle statuses. They describe how a child's process ended, never whether
 * its work succeeded.
 *
 * `sourceStatus` is `process_status` on process records (`process.py:_record_from_state`
 * binds `source_status=state.process_status`), so it holds one of these. It must never be
 * read as a work verdict: `waiting_reply` is what a *finished, healthy* child looks like
 * while its process waits for the next message, and it is in no success vocabulary.
 *
 * The trap is that `source_status` means something else for *workflow* records
 * (`workflow.py:263` binds the run status there), where it genuinely is a work verdict.
 * One field with two meanings cannot be trusted for either engine, so the verdict is read
 * from fields that are unambiguous: `metadata.completionStatus`, then `turnStatus`.
 */
const PROCESS_LIFECYCLE_STATUSES = new Set([
  'alive',
  'exited',
  'shutdown',
  'killed',
  'waiting_reply',
  'not_found',
  'starting',
  'stopping',
])

/**
 * Turn a bridge record into an announcement, or `null` when there is nothing to say.
 *
 * Non-terminal records are silent on purpose: a child that is merely running would
 * otherwise announce itself on every snapshot, training the user to ignore the line that
 * is supposed to be noticeable.
 *
 * Success is decided from the *work* status, which is `metadata.completionStatus` or
 * `metadata.turnStatus` -- never from `sourceStatus`, and never from the projected
 * `status` alone.
 *
 * `sourceStatus` is `process_status` for process records (`process.py` binds
 * `source_status=state.process_status`): `waiting_reply`, `exited`, `shutdown`, `killed`.
 * None of those is in any success vocabulary, so reading it as the work verdict announces
 * every finished child as failed. That happened twice: first with `killed`, then with
 * `waiting_reply` for three children that had all completed. See
 * `subagentWorkVerdict.test.ts` for the real captured record.
 *
 * For *workflow* records `sourceStatus` really does hold the run status
 * (`workflow.py:263`), so the same field means two different things. Asking the
 * unambiguous fields covers both engines without knowing which one produced the record.
 */
export function agentNoticeFromRecord(record: AgentRecordLike): AgentNotice | null {
  const status = String(record.status || '').toLowerCase()
  if (!AGENT_TERMINAL_STATUSES.has(status)) return null
  const executionId = String(record.executionId || '')
  if (!executionId) return null

  const meta = record.metadata || {}
  const metaCompletion = String(
    (typeof meta.completionStatus === 'string' && meta.completionStatus) ||
      (typeof meta.completion_status === 'string' && meta.completion_status) ||
      '',
  ).toLowerCase()
  const turnStatus = String(
    (typeof meta.turnStatus === 'string' && meta.turnStatus) ||
      (typeof meta.turn_status === 'string' && meta.turn_status) ||
      '',
  ).toLowerCase()
  const sourceStatus = String(record.sourceStatus || '').toLowerCase()

  // The work verdict, most authoritative first:
  //   1. `metadata.completionStatus` -- the raw work verdict, when the bridge sends it
  //   2. `metadata.turnStatus`       -- the turn's own end state
  //   3. `sourceStatus`              -- ONLY when it is not a process lifecycle word. For
  //      workflow records this really is the run status (`workflow.py:263`); for process
  //      records it is `process_status`, whose vocabulary never contains a work verdict.
  //   4. `status`                    -- the projection, which is itself a work projection
  //      on both engines (`_project_status`, `_run_status`).
  //
  // One chain, one check. The lifecycle filter is load-bearing rather than defensive: drop
  // it and `waiting_reply` becomes the verdict and a finished child reads as a failure.
  // That is the bug, and the mutation check proves this line is what stops it.
  const workStatus = metaCompletion
    || turnStatus
    || (sourceStatus && !PROCESS_LIFECYCLE_STATUSES.has(sourceStatus) ? sourceStatus : '')
    || status

  // "No verdict yet" is not a verdict. A child that has not finished a turn carries
  // `completionStatus: pending` (and `turnStatus: running`, which `_refresh_state`
  // rewrites from a pending turn when the process is gone). The chain below is a
  // POSITIVE check on COMPLETED_STATUSES, so every unrecognised word -- `pending`
  // included -- would fall through to `failed` and flash a red
  // `✗ Subagent <name> failed` the instant a child starts.
  //
  // Staying silent is the correct answer here: there is nothing to tell the user
  // yet, and a later snapshot carries the real verdict. Announcing a verdict the
  // record does not have is worse than announcing nothing.
  if (workStatus === 'pending' || workStatus === 'running' || workStatus === 'queued') {
    return null
  }

  const fromMeta =
    (typeof meta.taskName === 'string' && meta.taskName) ||
    (typeof meta.task_name === 'string' && meta.task_name) ||
    ''
  const fromPath = record.agentPath
    ? String(record.agentPath).split('/').filter(Boolean).pop() || ''
    : ''
  const name = String(fromMeta || fromPath || executionId)

  // The verdict has to be a positive success word: `failed`, `errored`, `killed`,
  // `interrupted` and anything unexpected fall through to "not ok". There is deliberately
  // no separate failure list -- success words cover every other outcome, so a second list
  // would be unreachable.
  const ok = COMPLETED_STATUSES.has(workStatus)
  const word = ok ? 'completed' : status === 'degraded' ? 'degraded' : 'failed'
  const detail =
    typeof meta.summary === 'string' && meta.summary.trim()
      ? meta.summary.trim()
      : typeof meta.error === 'string' && meta.error.trim()
        ? meta.error.trim()
        : undefined

  return { executionId, name, ok, word, detail }
}

export type AgentNoticeLike = AgentRecordLike

/** How many announcements the chrome keeps on screen at once. */
export const MAX_AGENT_NOTICES = 6

export type NoticeMerge = {
  /** What to display now, oldest first. */
  notices: AgentNotice[]
  /** True when the caller should `setState`; false means nothing visibly changed. */
  changed: boolean
  /**
   * Every execution id ever announced in this session.
   *
   * The caller must keep this across turns. It is deliberately *not* derivable from
   * `notices`, because the display is cleared at turn end while the announcement has
   * still happened -- see `mergeAgentNotices`.
   */
  announced: Set<string>
}

/**
 * Fold a bridge snapshot into the announcement list.
 *
 * The bug this exists to prevent: `_emit_agent_snapshot` sends
 * `list_records(include_terminal=True)` -- *every* terminal record in the session's
 * workflow store, on every snapshot, forever. The merge used to derive "already
 * announced" from the currently displayed notices, which are cleared at turn end. So
 * the first snapshot of the next turn saw an empty list, treated all those old terminal
 * records as new, and repainted them. In a real session that meant workflow children
 * from an earlier turn reappearing over an agent-team turn and staying for minutes.
 *
 * `announced` is therefore an *input*: a set that survives the display being cleared. An
 * id in it is never announced again. `notices` alone decides what is currently painted.
 *
 * Note what is deliberately NOT keyed into `announced`: the verdict. A child that first
 * reports `closed` and then `succeeded` is announced once, at `succeeded`. Refinements
 * to an *already displayed* line still go through, so a line cannot get stuck on a stale
 * verdict.
 */
export function mergeAgentNotices(
  current: AgentNotice[],
  records: readonly AgentRecordLike[],
  announced: ReadonlySet<string>,
): NoticeMerge {
  const announcedNext = new Set(announced)
  const byExecution = new Map<string, AgentNotice>()
  for (const notice of current) byExecution.set(notice.executionId, notice)
  let changed = false

  for (const record of records) {
    const notice = agentNoticeFromRecord(record)
    if (!notice) continue
    const displayed = byExecution.get(notice.executionId)
    if (displayed) {
      // Already on screen: let a refined verdict replace the stale one.
      if (displayed.ok === notice.ok && displayed.word === notice.word) continue
      byExecution.set(notice.executionId, notice)
      changed = true
      continue
    }
    // Not on screen. If it was announced in an earlier turn, stay silent -- this is
    // exactly the stale-workflow-replay case.
    if (announcedNext.has(notice.executionId)) continue
    announcedNext.add(notice.executionId)
    byExecution.set(notice.executionId, notice)
    changed = true
  }

  if (!changed) return { notices: current, changed: false, announced: announcedNext }

  const next = [...byExecution.values()]
  return {
    // Oldest first, bounded: a long session must not grow the chrome forever, and older
    // announcements have already been read. The bound is on the *display* only --
    // `announced` keeps every id, or a trimmed one would re-announce when the list
    // had room again.
    notices: next.length > MAX_AGENT_NOTICES ? next.slice(-MAX_AGENT_NOTICES) : next,
    changed: true,
    announced: announcedNext,
  }
}

/** The rendered line. Green+bold for success, error+bold for anything else. */
export function agentNoticeLine(notice: AgentNotice, successColor: string) {
  const color = notice.ok ? successColor : 'red'
  const text = `${notice.ok ? '✓' : '✗'} Subagent ${notice.name} ${notice.word}${
    notice.detail ? ` · ${notice.detail}` : ''
  }`
  return React.createElement(Text, { color, bold: true }, text)
}
