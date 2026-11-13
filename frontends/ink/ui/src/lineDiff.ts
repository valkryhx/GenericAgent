/**
 * 行级 diff：把「整帧重写」换成「只重写变化的行」。
 *
 * ── 为什么需要它 ──────────────────────────────────────────────────────
 *
 * ink@5.2.1 的 log-update 每帧写 `eraseLines(prevN) + output`，即**擦掉上一帧所有行、
 * 再整块重写**。实测（`probes/line_diff_verify.ts`，100x30）：
 *
 *   流式输出   757 B/帧 → 可降到 254 B/帧
 *   打字+回退  839 B/帧 → 可降到  29 B/帧
 *
 * 而 ink 内部其实建了完整 cell 网格（`Output`），只是 `output.get()` 立刻把它摊平成
 * 字符串，diff 需要的中间表示在交到调用方之前就被销毁了 —— 所以 ink 里没有挂点。
 * 但 GA 的 stdout 包裹层（`stdoutCursorPark`）每帧都能看到完整帧字符串，
 * 因此可以在**不改 ink 源码**的前提下做行级 diff。
 *
 * ── 为什么是「行级」而不是「cell 级」 ────────────────────────────────
 *
 * cell 级（Claude Code fork 的做法）需要 fork ink 的 renderer/output/log-update。
 * 行级拿不到两项收益，但都不影响本次目标：
 *   - 一行里只改几个字符时仍要重写整行（打字场景实测已降到 29 B/帧，可忽略）；
 *   - 整屏滚动交给终端硬件做（DECSTBM）—— GA 默认 inline 模式，收益本就小。
 *
 * ── 安全边界（必须退化，不能硬 diff）────────────────────────────────
 *
 * 1. **变矮**（`next.length < prev.length`）→ 返回 `null`。删掉的行位于帧内容之下，
 *    靠相对移动「向上重画」无法可靠清除；ink 自己的变矮分支也另有一套几何。
 * 2. **clearTerminal**（含 `ESC[2J`）→ 解析失败，调用方原样透传并重置记账。
 * 3. **含 `<Static>` 输出的突发**（一次突发里出现两段擦除）→ 解析失败，原样透传。
 *    静态区写入会推进 scrollback，帧的锚点变了，不能按旧几何 diff。
 * 4. **非标准帧**（没有 eraseLines 前缀，例如 `debug: true` 的输出）→ 解析失败。
 *
 * 边界 2~4 统一由「解析失败 ⇒ 本帧全量重写」兜住，不需要调用方逐个判断。
 *
 * ── 向下移动为什么用 `\r\n` 而不是 `CSI B` ───────────────────────────
 *
 * 实测教训：虚拟终端（`virtualScreen`）把 `\n` 和 `CSI B` 都当成「行号 +1」，
 * 所以单测/探针**看不出二者区别**。但在真实终端上，光标已在最后一行时
 * `\n` 会触发滚动（把顶行推进 scrollback），而 `CSI B` 只是钳位不动。
 * ink 自己的全量重写走的就是换行，帧变高时依赖这个滚动行为。
 * 因此向下移动必须用 `\r\n` 保持与 ink 一致的滚动语义；向上移动用 `CSI A` 无此问题。
 */

/** 行首归零：防止上一行遗留的样式（含背景色）漏进 `ESC[2K` 的擦除与本次重写。 */
const RESET = '\u001b[0m'

/** 擦除整行（erase in line, all）。 */
const ERASE_LINE = '\u001b[2K'

/**
 * 一帧的前缀：ink 的 `eraseLines(N)` = `ESC[2K (ESC[1A ESC[2K)* ESC[G`。
 *
 * 前缀可以是**空的**：ink 首帧的 `previousLineCount === 0`，`eraseLines(0)` 返回空串，
 * 整帧就只有 `output`。这种情况同样可以 diff（上一帧视为 0 行）。
 *
 * 负向断言要求内容里**不含** `ESC[2K`，否则说明这次突发里有多段擦除
 * （`log.clear` + `<Static>` + 主帧），几何已变，必须原样透传。
 */
const FRAME_PREFIX = /^(?:\u001b\[2K(?:\u001b\[1A\u001b\[2K)*\u001b\[0?G)?((?:(?!\u001b\[2K)[\s\S])*)$/

/** clearTerminal（`ESC[2J` / `ESC[3J`）：整屏重置，几何完全重建，不能 diff。 */
const CLEAR_TERMINAL = /\u001b\[[23]J/

/**
 * 把一次完整写出解析成「帧内容行」。
 *
 * 返回 `null` 表示**这一帧不能 diff**，调用方应原样透传。
 * 注意返回的行数组已去掉 ink 末尾那个 `\n` 造成的空串
 * （ink 的 `output = body + '\n'`，所以 `split('\n')` 末尾恒多一格）。
 */
export function parseFrameRows(body: string): string[] | null {
  if (CLEAR_TERMINAL.test(body)) return null
  if (body.length === 0) return null
  const match = FRAME_PREFIX.exec(body)
  if (!match) return null
  const rows = match[1]!.split('\n')
  if (rows.length > 0 && rows[rows.length - 1] === '') rows.pop()
  return rows
}

/**
 * 这次写出是否「看起来像 ink 的一帧」。
 *
 * 用于区分首帧（`eraseLines(0)` 空前缀，合法）与 `debug: true` 的连续输出
 * （既无擦除、也无帧尾换行，绝不能当作首帧去 diff）。
 *
 * 判据：要么带擦除前缀，要么以换行结尾（ink 的 `output = body + '\n'`）。
 */
export function looksLikeFrame(body: string): boolean {
  return body.includes('\u001b[2K') || body.endsWith('\n')
}

/**
 * 生成行级 diff 序列：从「上一帧帧底」出发，只重写变化的行。
 *
 * 返回 `null` 表示**不安全**，调用方必须退化为全量重写。触发条件：
 *   - 行数变化（变高或变矮）；
 *   - 帧高达到视口高度（见 §滚动语义）。
 *
 * 不变量：成功时（非 null）光标停在**本帧帧底**（内容最后一行之后、列 0），
 * 与全量重写完全一致，因此后续 park 段的相对上移仍然成立。
 *
 * ── §为什么不处理变高/变矮 ───────────────────────────────────────────
 *
 * 全量重写是「先擦干上一帧占的行，再从上往下重画」，档位固定在屏幕上。
 * 行级 diff 要改变行数，只能用 `\n` 或 `CSI B` 建行：
 *   - `CSI B` 在真实终端钳位在视口底部，建不出新行；
 *   - `\n` 到底部会滚动，把顶行推进 scrollback。
 * 两者都不等价于全量重写，所以行数一变就退化。
 *
 * ── §滚动语义（这条最隐蔽）──────────────────────────────────────────
 *
 * ink 写的是 `body + '\n'`，所以一帧占 `rows.length + 1` 行。当这个值达到视口高度
 * （`rows.length + 1 >= terminalRows`）时，**每帧全量重写的末尾换行都会触底滚动一次**，
 * 屏幕整体上移。而 diff 只重写变化的行、不写那些空行，不会触发滚动 —— 两路的屏幕
 * 内容会逐渐错开。
 *
 * 实测证据：`probes/line_diff_acceptance.ts` 在 100x30 下首次比对 28/33 帧不一致，
 * 加上带滚动的虚拟终端后仍然不一致，直到引入这条边界判断才归零。
 *
 * 因此帧高触顶时必须退化。`terminalRows` 未知（测试/非 TTY）时按无限高处理。
 */
export function buildLineDiff(
  prev: readonly string[],
  next: readonly string[],
  terminalRows = Number.POSITIVE_INFINITY,
): string | null {
  // 行数变化（变矮或变高）一律退化：见 §为什么不处理变高/变矮。
  if (next.length !== prev.length) return null
  // 帧高触顶：整帧（内容 + 尾部换行）刚好或超过视口高度时，全量重写的末尾换行
  // 会触底滚动，而 diff 不写那些空行、不滚动 → 两路的屏幕内容会逐渐错开。
  if (next.length + 1 >= terminalRows) return null

  let out = ''
  // 起点：上一帧帧底（= 本帧帧底，因为行数相同）。
  let cursor = prev.length

  for (let row = 0; row < next.length; row += 1) {
    const before = prev[row]!
    const after = next[row]!
    if (before === after) continue

    out += moveTo(row - cursor)
    // 先归零再擦：`ESC[2K` 用当前背景色填充，带背景的样式必须先清掉。
    out += `\r${RESET}${ERASE_LINE}${after}`
    cursor = row
  }

  // 回到本帧帧底。
  out += moveTo(next.length - cursor)
  out += '\r'
  return out
}

/**
 * 视口内的行间移动。
 *
 * 行数恒定的前提下，这只是「光标在已存在的行之间上下移动」，用 `CSI A` / `CSI B`
 * 都是安全的：不建行、不滚动。**绝不能用 `\n`** —— 光标已在视口底部时 `\n` 会
 * 滚动屏幕，而全量重写不滚动，两者就分叉了。
 */
function moveTo(dy: number): string {
  if (dy === 0) return ''
  return dy < 0 ? `\u001b[${-dy}A` : `\u001b[${dy}B`
}
