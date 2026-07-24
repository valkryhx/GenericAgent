/**
 * 路径 A 光标协调：包裹 ink 的 stdout，让「内容写入」与「caret 定位」成为同一个
 * writer 的串行两步，彻底消灭旧的 useLayoutEffect 旁路 CUP 与 ink log-update 的竞态。
 *
 * 机制（已由 cursorParkModel.test.ts 的确定性模拟验证）：
 * - ink 每帧写 `eraseLines(prevN) + output`，写完原生（隐藏）光标停在帧底
 *   （最后一行内容的下一行、col 0）——这是 ink 下一帧 eraseLines 往上擦的起点。
 * - PARK：帧写完后，用纯相对移动把光标从帧底上移到 caret：`ESC[<up>A` + `ESC[<col+1>G`。
 * - UNPARK：下一次写入前，先用相反的相对移动把光标移回帧底：`ESC[<up>B` + `\r`。
 *   因为 unpark 用「park 时记下的 up」原样下移，无论新帧几何如何，都精确回到 ink 期望的
 *   帧底，eraseLines 永远从帧底往上擦对行 → 无 ghost / 无整框漂移。
 *
 * 为什么 park 放进 microtask：ink 的 onRender 一次可能同步写 1~3 次（log.clear /
 * Static / 主帧），主帧永远是最后一次。microtask 在这串同步写入之后、下一次宏任务
 * （节流定时器/新渲染）之前触发，因此我们只在「本轮 onRender 的最后一次写入之后」park
 * 一次，无需去解析 chunk 区分主帧 / Static / clear。unpark 始终同步发生在每次写入开头，
 * 保证 ink 下一帧写入时光标已在帧底。
 *
 * caret 目标由 App 每次渲染写进 setPark(spec)：{ up, col } 相对帧底，或 null（运行中 /
 * 未就绪 → 不 park，光标留在帧底，与 ink 默认一致）。
 *
 * 原生光标可见性（关键修复，2026-07-16）：终端的 IME 候选框锚定在**可见**的原生光标 cell
 * 上（Windows Terminal 实测），而非隐藏光标。旧设计让原生光标保持隐藏、只靠 InputView 的
 * 反显块提供可见光标，导致中文合成时 IME 窗口无可锚定的可见光标 → 漂到屏幕右下角（英文因
 * 反显块是 GA 自绘故看起来正常）。参考 Claude Code：frame.cursor.visible=true、帧末 SHOW。
 * 因此本 writer 在 park 到 caret 后 SHOW 原生光标，在下一帧写入前（unpark）先 HIDE，
 * 使原生光标只在「已停在 caret」时可见，不会在帧底/写入过程中闪现。反显块保留作为兜底。
 */

const SHOW_CURSOR = '\x1b[?25h'
const HIDE_CURSOR = '\x1b[?25l'

export type ParkSpec = { up: number; col: number } | null

type Scheduler = (cb: () => void) => void

/** ink 只用到 stdout 的这些成员；测试里用最小 sink 即可。 */
type WritableLike = { write: (chunk: string, ...rest: unknown[]) => boolean }

const defaultScheduler: Scheduler = (cb) => {
  queueMicrotask(cb)
}

/**
 * 纯序列化核心：不含 Proxy，接收一个 sink（真实写出函数），便于单测断言写入顺序。
 */
export class CursorParkWriter {
  /** park 时上移的行数；null 表示当前未 park。 */
  parkedUp: number | null = null
  private currentSpec: ParkSpec = null
  private scheduled = false
  private disposed = false

  constructor(
    private readonly sink: (chunk: string) => void,
    private readonly schedule: Scheduler = defaultScheduler,
  ) {}

  /** App 每次渲染调用：传相对帧底的 caret（{up,col}），或 null 表示本帧不 park。 */
  setPark(spec: ParkSpec): void {
    this.currentSpec = spec
  }

  /** 包裹 ink/App 的每次 stdout.write：先 unpark，再写内容，最后（microtask）park。 */
  write(chunk: string): void {
    this.unpark()
    this.sink(chunk)
    this.schedulePark()
  }

  /** 停止 park（退出清理前调用），并清掉未决 microtask。 */
  dispose(): void {
    this.disposed = true
    this.currentSpec = null
  }

  /** 供非字符串透传路径：只把光标移回帧底，不安排 park。 */
  flushUnpark(): void {
    this.unpark()
  }

  private unpark(): void {
    if (this.parkedUp === null) return
    // 先 HIDE（park 时 SHOW 过），避免下面下移回帧底时可见光标在屏上滑动/在写入过程闪现；
    // 再下移 parkedUp 行回帧底、回列 0（ink 下一帧 eraseLines 的起点）。
    this.sink(this.parkedUp > 0 ? `${HIDE_CURSOR}\x1b[${this.parkedUp}B\r` : `${HIDE_CURSOR}\r`)
    this.parkedUp = null
  }

  private schedulePark(): void {
    if (this.scheduled) return
    this.scheduled = true
    this.schedule(() => {
      this.scheduled = false
      if (this.disposed) return
      // 若在 microtask 触发前又发生了写入，unpark 会把 parkedUp 清回 null；
      // 这里只在仍未 park 时执行一次。
      if (this.parkedUp !== null) return
      const spec = this.currentSpec
      if (!spec) return
      const up = spec.up > 0 ? `\x1b[${spec.up}A` : ''
      // 上移到 caret 行、定列后 SHOW：原生光标只在已停在 caret 时可见，IME 候选框据此锚定。
      this.sink(`${up}\x1b[${spec.col + 1}G${SHOW_CURSOR}`)
      this.parkedUp = spec.up
    })
  }
}

export type CursorParkController = {
  /** 传给 ink `render(<App/>, { stdout })` 的包裹流。 */
  stdout: NodeJS.WriteStream
  /** App 每次渲染调用，声明 caret 相对帧底位置（或 null）。 */
  setPark(spec: ParkSpec): void
  /** 退出清理前调用：若当前 park 在 caret，先相对移回帧底，让后续清理几何确定。 */
  unpark(): void
  dispose(): void
}

/**
 * 用 Proxy 包裹真实 stdout：仅拦截 write，其余属性（columns/rows/on/off…）透传给底层，
 * 使 ink 的 resize 监听、宽高读取一切照旧。
 */
export function createCursorParkStdout(base: NodeJS.WriteStream): CursorParkController {
  const writer = new CursorParkWriter((chunk) => {
    base.write(chunk)
  })

  const proxy = new Proxy(base, {
    get(target, prop, receiver) {
      if (prop === 'write') {
        return (chunk: unknown, ...rest: unknown[]): boolean => {
          // 非字符串（Buffer）或带回调的写入：先 unpark 回帧底以维持不变量，再原样透传。
          // 不安排新的 park（ink 只用字符串写主帧；此分支纯属防御）。
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
    dispose: () => writer.dispose(),
  }
}
