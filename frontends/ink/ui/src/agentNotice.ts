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

/** Statuses that mean "this child is done and the user should be told". */
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
  agentPath?: string | null
  metadata?: Record<string, unknown>
}

/**
 * Turn a bridge record into an announcement, or `null` when there is nothing to say.
 *
 * Non-terminal records are silent on purpose: a child that is merely running would
 * otherwise announce itself on every snapshot, training the user to ignore the line that
 * is supposed to be noticeable.
 */
export function agentNoticeFromRecord(record: AgentRecordLike): AgentNotice | null {
  const status = String(record.status || '').toLowerCase()
  if (!AGENT_TERMINAL_STATUSES.has(status)) return null
  const executionId = String(record.executionId || '')
  if (!executionId) return null

  const meta = record.metadata || {}
  const fromMeta =
    (typeof meta.taskName === 'string' && meta.taskName) ||
    (typeof meta.task_name === 'string' && meta.task_name) ||
    ''
  const fromPath = record.agentPath
    ? String(record.agentPath).split('/').filter(Boolean).pop() || ''
    : ''
  const name = String(fromMeta || fromPath || executionId)

  const ok = status === 'succeeded' || status === 'completed'
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
