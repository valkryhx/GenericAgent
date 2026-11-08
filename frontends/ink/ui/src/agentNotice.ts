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
 * Turn a bridge record into an announcement, or `null` when there is nothing to say.
 *
 * Non-terminal records are silent on purpose: a child that is merely running would
 * otherwise announce itself on every snapshot, training the user to ignore the line that
 * is supposed to be noticeable.
 *
 * Success is decided from the work status (`sourceStatus`, then `completionStatus` in the
 * metadata), never from the process status alone. The bridge projects `killed` before
 * `completed` (`core/control/process.py:_project_status`), so trusting `status` marks a
 * finished child as failed -- which is exactly what happened to a real session's
 * `probe_mid`: MID_ONE, MID_TWO and MID_DONE in its output file, announced in red.
 */
export function agentNoticeFromRecord(record: AgentRecordLike): AgentNotice | null {
  const status = String(record.status || '').toLowerCase()
  if (!AGENT_TERMINAL_STATUSES.has(status)) return null
  const executionId = String(record.executionId || '')
  if (!executionId) return null

  const meta = record.metadata || {}
  const workStatus = String(
    record.sourceStatus ||
      (typeof meta.completionStatus === 'string' && meta.completionStatus) ||
      (typeof meta.completion_status === 'string' && meta.completion_status) ||
      '',
  ).toLowerCase()

  const fromMeta =
    (typeof meta.taskName === 'string' && meta.taskName) ||
    (typeof meta.task_name === 'string' && meta.task_name) ||
    ''
  const fromPath = record.agentPath
    ? String(record.agentPath).split('/').filter(Boolean).pop() || ''
    : ''
  const name = String(fromMeta || fromPath || executionId)

  // Did the work succeed? The work status is authoritative when present. Without one the
  // projected status is all there is, and a bare `killed` then really does mean the child
  // was cut off rather than finished.
  const ok = workStatus
    ? COMPLETED_STATUSES.has(workStatus)
    : COMPLETED_STATUSES.has(status)
  const word = ok ? 'completed' : status === 'degraded' ? 'degraded' : 'failed'
  const detail =
    typeof meta.summary === 'string' && meta.summary.trim()
      ? meta.summary.trim()
      : typeof meta.error === 'string' && meta.error.trim()
        ? meta.error.trim()
        : undefined

  return { executionId, name, ok, word, detail }
}

/** The rendered line. Green+bold for success, error+bold for anything else. */
export function agentNoticeLine(notice: AgentNotice, successColor: string) {
  const color = notice.ok ? successColor : 'red'
  const text = `${notice.ok ? '✓' : '✗'} Subagent ${notice.name} ${notice.word}${
    notice.detail ? ` · ${notice.detail}` : ''
  }`
  return React.createElement(Text, { color, bold: true }, text)
}
