/**
 * 路径 A 光标协调：包裹 ink 的 stdout，把「一次 onRender 突发的全部写入」攒成
 * **一次原子写入**，并在其中完成 caret 定位。
 *
 * ── 为什么是「攒成一次」（P0，2026-11-12）────────────────────────────────
 *
 * ink@5.2.1 的 log-update 每帧写 `eraseLines(prevN) + output`，是一个
 * 「先把动态区擦空、再整块重画」的两段式动作。旧实现把它和 unpark / park 拆成
 * 三次独立的 base.write：
 *
 *     write(unpark)  →  HIDE + ESC[nB + \r
 *     write(frame)   →  eraseLines(N) + output        ← 动态区此刻是空的
 *     park           →  ESC[nA + ESC[mG + SHOW        ← 光标此刻才落位
 *
 * 三次写入之间终端可以刷新屏幕，于是「擦空但未重画」的中间态被画出来 —— 真机上
 * 表现为「光标下穿到输入框下侧线」。实测该链路从未使用过同步输出
 * （`probes/cursor_toggle_cost.ts`：`2026h/l = 0 / 0`）。
 *
 * 现在整条突发先进缓冲区，在 microtask 里用 DEC 2026 包住一次性写出：
 *
 *     BSU + [unpark] + [clear] + [static] + [frame] + [park] + ESU
 *
 * 终端要么整块显示、要么整块不显示，中间态不可见。
 *
 * ── 为什么 microtask 是对的时机 ────────────────────────────────────────
 *
 * ink 的 onRender 由 reconciler 的 resetAfterCommit 触发，一次 onRender 内可能同步写
 * 1~3 次（log.clear / <Static> / 主帧），主帧永远是最后一次。microtask 在「本轮所有
 * 同步写入之后、下一次宏任务之前」执行，因此：
 *   - 一次突发的全部写入都已入缓冲 → 只产生一次 base.write；
 *   - React 的 layout effect（App 的 setPark 在那里）已在同一 commit 的同步阶段完成
 *     → 读到的是**本次** commit 的 park 规格，无代次滞后。
 *     （探针 `probes/park_stale_race.ts` 实测 stale = 0；这也正是本实现此前把 park
 *     放进 microtask 的既有收益，现在把适用范围扩大到整帧。）
 *
 * ── 光标可见性 ───────────────────────────────────────────────────────
 *
 * 终端的 IME 候选框锚定在**可见**的原生光标 cell 上（Windows Terminal 实测），所以
 * 光标停在 caret 时必须 SHOW，否则中文合成时候选框漂到屏幕右下角（2026-07-16 修复）。
 *
 * HIDE 的唯一职责是「别让人看见光标从 caret 滑回帧底」。**原子提交之后，帧底位置
 * 永远不会被显示** —— 但前提是这一次事务**结尾确实会重新 park 回 caret**。若本帧不
 * park（面板打开 / 未就绪），光标最终会停在帧底并且可见，那就必须 HIDE。
 * 因此规则是：
 *
 *   needHide = cursorShown && (非同步输出 || 本帧不 park)
 *   needShow = !cursorShown && 本帧 park
 *
 * 同步输出生效且本帧要 park 时 → 全程不翻转（旧实现实测流式中 8.4~11.2 次翻转对/秒，
 * 肉眼即「光标一直在闪」）。同步输出不可用时退回旧行为（每帧 HIDE → SHOW）。
 *
 * 不变量：**光标可见 ⟺ 它正停在 caret 上。**
 */

import { resetViewportSequence } from './terminalCleanup.js'
import { isSynchronizedOutputSupported, wrapSynchronizedUpdate } from './syncOutput.js'
import { buildLineDiff, looksLikeFrame, parseFrameRows } from './lineDiff.js'

const SHOW_CURSOR = '\u001b[?25h'
const HIDE_CURSOR = '\u001b[?25l'

export type ParkSpec = { up: number; col: number } | null

type Scheduler = (cb: () => void) => void

/** ink 只用到 stdout 的这些成员；测试里用最小 sink 即可。 */
type WritableLike = { write: (chunk: string, ...rest: unknown[]) => boolean }

const defaultScheduler: Scheduler = (cb) => {
  queueMicrotask(cb)
}

export type CursorParkOptions = {
  /** 是否使用 DEC 2026 同步输出。默认按终端能力探测。 */
  syncOutput?: boolean
  /** 是否启用行级 diff。默认开启。 */
  lineDiff?: boolean
  /** 终端行数，用于判断「帧高触顶 → 必须退化」。默认从 base stdout 读取。 */
  terminalRows?: number
}

/**
 * 纯序列化核心：不含 Proxy，接收一个 sink（真实写出函数），便于单测断言写入顺序。
 *
 * 写入模型：`write()` 只入缓冲；`flush()`（由 microtask 触发）一次性合并写出。
 */
export class CursorParkWriter {
  /** park 时上移的行数；null 表示当前未 park（光标不在 caret）。 */
  parkedUp: number | null = null
  /** 原生光标当前是否可见（DECTCEM 记账）。 */
  cursorShown = false
  /** 是否用 BSU/ESU 包住每次写出。 */
  readonly syncOutput: boolean
  /** 是否启用了行级 diff。 */
  readonly lineDiffEnabled: boolean
  private currentSpec: ParkSpec = null
  private scheduled = false
  private disposed = false
  /** 每次整屏重置 +1：让已排队但尚未执行的 flush 失效。 */
  private parkEpoch = 0
  /** 待写出的字节。一次 onRender 突发的全部写入都攒在这里。 */
  private buffer = ''
  /**
   * 上一帧的**完整内容行**，行级 diff 的唯一依据。
   *
   * `null` 表示「不知道屏幕上现在是什么」——此时**绝不能 diff**，只能原样透传
   * （原样透传本身就是全量重写，永远正确）。会在以下情况变为 null：
   *   - 还没画过任何一帧；
   *   - 这一帧解析失败（clearTerminal / 多段擦除 / debug 输出）；
   *   - 整屏重置。
   */
  private prevRows: string[] | null = null

  /** 仅在调试时输出：设置 `GA_UI_PARK_DEBUG=1` 打印每帧的解析与 diff 决策。 */
  private readonly debug: boolean
  /** 终端行数；帧高触顶时 diff 会退化（见 lineDiff.ts §滚动语义）。 */
  private readonly terminalRows: number

  constructor(
    private readonly sink: (chunk: string) => void,
    private readonly schedule: Scheduler = defaultScheduler,
    options: CursorParkOptions = {},
  ) {
    this.syncOutput = options.syncOutput ?? isSynchronizedOutputSupported()
    this.lineDiffEnabled = options.lineDiff ?? true
    this.debug = (process.env.GA_UI_PARK_DEBUG ?? '') === '1'
    this.terminalRows = options.terminalRows ?? Number.POSITIVE_INFINITY
  }

  /** App 每次渲染调用：传相对帧底的 caret（{up,col}），或 null 表示本帧不 park。 */
  setPark(spec: ParkSpec): void {
    this.currentSpec = spec
  }

  /**
   * ink/App 的每次 stdout.write：只入缓冲，由 microtask 合并写出。
   *
   * 内容**不**在这里下发给 sink —— 否则就回到「一帧多次 write」的老问题。
   */
  write(chunk: string): void {
    this.buffer += chunk
    this.scheduleFlush()
  }

  /**
   * 把缓冲区一次性写出（含 unpark/park），并清空。
   *
   * @param park 是否在本帧结尾 park 回 caret。`false` 用于退出清理（只要回帧底）。
   */
  flush(park = true): void {
    this.scheduled = false
    if (this.disposed) {
      this.buffer = ''
      return
    }

    const body = this.buffer
    this.buffer = ''

    const spec = park ? this.currentSpec : null
    const willPark = spec !== null

    // ── 行级 diff ────────────────────────────────────────────────────
    //
    // 只在「上一帧内容已知」且「本帧可解析」时替换成 diff 序列；任何不确定一律保留
    // 原始字节 —— 原样透传就是全量重写，永远正确，diff 只是省字节的可选优化。
    const frameBody = this.recognizeFrame(body)
    const replacement = this.computeLineDiff(frameBody)
    const emittedBody = replacement === null ? body : replacement

    // unpark 段：把光标从 caret 移回帧底（ink 下一帧相对于帧底擦除）。
    let prefix = ''
    if (this.parkedUp !== null) {
      const needHide = this.cursorShown && (!this.syncOutput || !willPark)
      prefix = `${needHide ? HIDE_CURSOR : ''}${this.parkedUp > 0 ? `\x1b[${this.parkedUp}B` : ''}\r`
      if (needHide) this.cursorShown = false
      this.parkedUp = null
    } else if (!willPark && this.cursorShown) {
      // 没有 unpark 段可挂（当前不在 caret），但本帧结束后光标会停在帧底且可见 → 遮蔽。
      prefix = HIDE_CURSOR
      this.cursorShown = false
    }

    // park 段：上移到 caret 行、定列，必要时 SHOW。
    let suffix = ''
    if (spec) {
      const up = spec.up > 0 ? `\x1b[${spec.up}A` : ''
      // 只在「需要变可见」时发 SHOW：同步输出生效时光标全程可见，这里通常一次都不发。
      const needShow = !this.cursorShown
      suffix = `${up}\x1b[${spec.col + 1}G${needShow ? SHOW_CURSOR : ''}`
      if (needShow) this.cursorShown = true
      this.parkedUp = spec.up
    }

    const payload = `${prefix}${emittedBody}${suffix}`
    if (payload.length === 0) return
    this.sink(wrapSynchronizedUpdate(payload, this.syncOutput))
  }

  /**
   * 判断整段突发是否「恰好是一帧」，是则返回它的字节，否则 `null`。
   *
   * 一次突发里可能夹着 App 自己写的非帧字节（清屏序列、鼠标模式切换、OSC 剪贴板…），
   * 也可能同时含 `log.clear()` 与 `<Static>` 两段。这些都会让帧锚点不确定，
   * 必须整体放弃 diff（返回 null → 调用方原样透传，等价于全量重写，永远正确）。
   */
  private recognizeFrame(body: string): string | null {
    if (!looksLikeFrame(body)) return null
    // parseFrameRows 的负向断言已排除：clearTerminal、多段擦除、无帧尾换行。
    return parseFrameRows(body) === null ? null : body
  }

  /**
   * 计算行级 diff。返回 `null` 表示「不替换，原样写出」。
   *
   * 无论是否替换，都会按本帧内容更新 `prevRows` —— 除非本帧不可解析（此时清空记账，
   * 因为屏幕上发生了什么已经不确定）。
   */
  private computeLineDiff(frameBody: string | null): string | null {
    if (frameBody === null) {
      // 本帧不可解析：不确定屏幕状态，放弃 diff 直到下一帧重建。
      this.prevRows = null
      return null
    }
    const next = parseFrameRows(frameBody)!
    if (!this.lineDiffEnabled) {
      this.prevRows = next
      return null
    }
    const prev = this.prevRows
    // 首次（不知道上一帧）→ 原样透传，但记住本帧，从下一帧起可以 diff。
    if (prev === null) {
      this.prevRows = next
      return null
    }
    const diff = buildLineDiff(prev, next, this.terminalRows)
    this.prevRows = next
    if (this.debug) {
      const changed = prev.reduce((a, r, i) => a + (r !== next[i] ? 1 : 0), 0)
      // eslint-disable-next-line no-console
      console.error(
        `[park] prev=${prev.length} next=${next.length} rows=${this.terminalRows} changedRows=${changed} ` +
        `→ ${diff === null ? 'FALLBACK(全量)' : `diff ${diff.length}B`}`,
      )
    }
    // null = 行数变化，不安全 → 原样透传（即全量重写）。
    return diff
  }

  /** 停止 park（退出清理前调用），并清掉未决 microtask。 */
  dispose(): void {
    this.disposed = true
    this.currentSpec = null
    this.buffer = ''
    this.prevRows = null
  }

  /**
   * 终端 reflow（宽度变化）后的整屏重置：写出「清屏 + 清 scrollback + 归位」，并丢弃
   * park 记账。
   *
   * 必须同时作废未决的 flush 并清掉 parkedUp / 缓冲区：光标已被 CSI H 归位到屏幕左上，
   * 若下一次写入仍按旧的 parkedUp 下移（unpark），整帧会写到错误位置。
   */
  resetViewport(sequence: string): void {
    this.parkEpoch += 1
    this.scheduled = false
    this.parkedUp = null
    this.buffer = ''
    // 整屏已被清空并归位，屏幕内容不再与 prevRows 对应 → 丢弃记账，下一帧重建。
    this.prevRows = null
    // 归位序列与随后的全量重绘是两段，中间态同样不该被看见。
    this.sink(wrapSynchronizedUpdate(sequence, this.syncOutput))
    // CSI H 不改变 DECTCEM，可见性记账保持不变。
  }

  /** 退出清理：把缓冲写出并把光标移回帧底，**不**重新 park。 */
  flushUnpark(): void {
    this.flush(false)
  }

  private scheduleFlush(): void {
    if (this.scheduled) return
    this.scheduled = true
    const epoch = this.parkEpoch
    this.schedule(() => {
      // 期间发生过整屏重置（终端宽度变化）→ 这次 flush 的几何已经失效，丢弃。
      if (this.parkEpoch !== epoch) return
      this.flush()
    })
  }
}

export type CursorParkController = {
  /** 传给 ink `render(<App/>, { stdout })` 的包裹流。 */
  stdout: NodeJS.WriteStream
  /** App 每次渲染调用，声明 caret 相对帧底位置（或 null）。 */
  setPark(spec: ParkSpec): void
  /** 退出清理前调用：把缓冲区写出，并相对移回帧底让后续清理几何确定。 */
  unpark(): void
  /** 终端宽度变化（reflow）时调用：整屏重置 + 丢弃 park 记账，随后由 App 触发全量重绘。 */
  resetViewport(): void
  /** 立即写出所有缓冲内容（测试与退出路径用）。 */
  flush(): void
  /** 是否启用了同步输出。 */
  syncOutput: boolean
  dispose(): void
}

/**
 * 用 Proxy 包裹真实 stdout：仅拦截 write，其余属性（columns/rows/on/off…）透传给底层，
 * 使 ink 的 resize 监听、宽高读取一切照旧。
 */
export function createCursorParkStdout(
  base: NodeJS.WriteStream,
  options: CursorParkOptions = {},
): CursorParkController {
  const writer = new CursorParkWriter((chunk) => {
    base.write(chunk)
  }, defaultScheduler, {
    ...options,
    // 终端行数默认从真实 stdout 读取；测试里传的 fake stream 也带 rows。
    terminalRows: options.terminalRows ?? (base.rows || Number.POSITIVE_INFINITY),
  })

  const proxy = new Proxy(base, {
    get(target, prop) {
      if (prop === 'write') {
        return (chunk: unknown, ...rest: unknown[]): boolean => {
          // 非字符串（Buffer）或带回调的写入：先把已缓冲的内容写出以维持顺序，
          // 再原样透传。
          if (typeof chunk !== 'string') {
            writer.flushUnpark()
            return (target.write as WritableLike['write']).call(target, chunk as string, ...(rest as never[]))
          }
          writer.write(chunk)
          return true
        }
      }
      const value = Reflect.get(target, prop, target)
      return typeof value === 'function' ? value.bind(target) : value
    },
  }) as unknown as NodeJS.WriteStream

  return {
    stdout: proxy,
    setPark: (spec) => writer.setPark(spec),
    unpark: () => writer.flushUnpark(),
    resetViewport: () => writer.resetViewport(resetViewportSequence()),
    flush: () => writer.flush(),
    syncOutput: writer.syncOutput,
    dispose: () => writer.dispose(),
  }
}
