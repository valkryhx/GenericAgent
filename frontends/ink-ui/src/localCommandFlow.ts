import type { BridgeEvent } from './protocol.js'

export function pendingLocalCommandAfterBridgeEvent(
  pendingCommand: string | null,
  event: BridgeEvent,
): string | null {
  // 本地命令结果到达后结束 pending；history_replace 在 App 里单独清 pending
  // （compact 成功只走 replace，失败仍走 local_command_output）。
  if (event.type === 'local_command_output') return null
  return pendingCommand
}

/**
 * /stop 回显是否应写入 transcript。
 * 同一 stop 周期内只回显一次，避免双 Enter / 重入 applyInputDecision
 * 在 Static 里留下两份 `/stop` + `Stop requested`。
 */
export function shouldEchoStopTranscript(alreadyEchoedThisStopCycle: boolean): boolean {
  return !alreadyEchoedThisStopCycle
}

/**
 * stop 回显闸门何时重置。
 *
 * 只在进入 `running`（新任务真正开始）时打开闸门。
 * 旧逻辑在 `idle` 重置是错的：abort 很快回到 idle 后，双 Enter 的第二下
 * 会再写一对 `/stop` + `Stop requested`（用户截图 stop.png 的症状）。
 */
export function stopEchoGateAfterStatus(status: string | undefined | null): 'reset' | 'keep' {
  return status === 'running' ? 'reset' : 'keep'
}

/** 把「是否回显」与「闸门下一状态」合成纯函数，便于单测状态机。 */
export function nextStopEchoGate(args: {
  alreadyEchoed: boolean
  isStopCommand: boolean
}): { echo: boolean; nextEchoed: boolean } {
  if (!args.isStopCommand) {
    return { echo: false, nextEchoed: args.alreadyEchoed }
  }
  if (!shouldEchoStopTranscript(args.alreadyEchoed)) {
    return { echo: false, nextEchoed: true }
  }
  return { echo: true, nextEchoed: true }
}
