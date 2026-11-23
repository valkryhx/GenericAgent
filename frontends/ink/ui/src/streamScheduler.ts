type StreamSchedulerOptions<T> = {
  intervalMs: number
  leading: boolean
  dispatch: (taskId: number, text: string) => void
  now: () => number
  schedule: (callback: () => void, delay: number) => T
  cancel: (handle: T) => void
}

export function createStreamScheduler<T>({ intervalMs, leading, dispatch, now, schedule, cancel }: StreamSchedulerOptions<T>) {
  const pending = new Map<number, string>()
  const lastDispatch = new Map<number, number>()
  let timer: T | undefined

  function cancelTimer() {
    if (timer !== undefined) cancel(timer)
    timer = undefined
  }

  function send(taskId: number, text: string) {
    lastDispatch.set(taskId, now())
    dispatch(taskId, text)
  }

  function flush() {
    cancelTimer()
    const batch = [...pending].sort(([a], [b]) => a - b)
    pending.clear()
    for (const [taskId, text] of batch) send(taskId, text)
  }

  return {
    push(taskId: number, text: string) {
      if (!text) return
      const last = lastDispatch.get(taskId)
      const wait = last === undefined ? 0 : Math.max(0, intervalMs - (now() - last))
      if (leading && wait === 0 && !pending.has(taskId)) {
        send(taskId, text)
        return
      }
      pending.set(taskId, (pending.get(taskId) ?? '') + text)
      if (timer === undefined) timer = schedule(flush, leading ? wait : intervalMs)
    },
    flush,
    finish(taskId: number) {
      flush()
      lastDispatch.delete(taskId)
    },
    clear() {
      cancelTimer()
      pending.clear()
      lastDispatch.clear()
    },
  }
}
