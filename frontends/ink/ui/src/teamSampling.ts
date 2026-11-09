/**
 * When may a live `/team` sampling loop stop?
 *
 * Run 3 of the 15-task scale e2e exited after four quiet samples while the lead was still
 * handing tasks 6-15 to its workers: every worker was alive mid-turn, but the roster had
 * not changed yet, so a naive "completed didn't move" counter declared a stall. A worker
 * that is still alive is the authority on whether the board is stuck, not the counter.
 *
 * Pure so the rule can be unit-tested without a model, a bridge, or a terminal.
 */
export const SAMPLING_STALL_LIMIT = 20

export type SamplingProgress = {
  completed: number
  inProgress: number
  pending: number
  liveWorkers: number
  leadIdle: boolean
  total: number
}

export type SamplingState = { key: string; stalled: number }

export function samplingKey(progress: SamplingProgress): string {
  return `${progress.completed}:${progress.inProgress}:${progress.pending}:${progress.liveWorkers}`
}

export function advanceSampling(
  previous: SamplingState,
  progress: SamplingProgress,
): { state: SamplingState; stop: 'done' | 'stalled' | null } {
  const key = samplingKey(progress)
  const stalled = key === previous.key ? previous.stalled + 1 : 0
  const state = { key, stalled }
  if (progress.leadIdle && progress.completed >= progress.total) return { state, stop: 'done' }
  if (progress.leadIdle && progress.liveWorkers === 0 && stalled >= SAMPLING_STALL_LIMIT) {
    return { state, stop: 'stalled' }
  }
  return { state, stop: null }
}
