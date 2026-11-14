/**
 * 选区坐标空间：屏幕行 ↔ 内容行。
 *
 * ## 为什么需要这一层（用户实测，2026-11-14）
 *
 * 用户原话：「我现在确实可以在源码启动后 ga 的 ui 上 左键拖动来复制内容，
 * 但是无法选出屏幕外需要滑动的区域，也就是无法自动随着滑动来上下扩展屏幕」。
 *
 * 根因有两层：
 *
 * 1. **拖动没有自动滚动**。`handleMouseEvent` 的 drag 分支只更新焦点，
 *    拖到视口边缘就停住了，不会把 transcript 往上/往下滚。
 * 2. **更根本的一层：选区存在屏幕坐标里**。`frameRows` 只包含**当前可见**
 *    的行，所以即使滚动了，滚出去的内容也不在任何一帧里，「选中屏幕外的
 *    内容」在模型上就不可能成立。
 *
 * 修法：选区改存**内容坐标**（= 整条 transcript 的绝对行号，与滚动无关），
 * 屏幕坐标只用于画高亮。这样：
 * - 滚动不会让 anchor 指的文本变化（内容行号不随滚动改变）；
 * - 复制时用「内容帧」（含全部 transcript 行）取文本，屏幕外也能取到。
 *
 * ## 坐标约定
 *
 * 内容行：
 * - `0 .. h-1`            header
 * - `h .. h+total-1`      整条 transcript
 * - `h+total ..`          band（goal 卡 / 活动行 / 通知）
 * - 之后                   composer
 *
 * 屏幕行：header 同上；transcript 只占视口行数 `v`；band/composer 紧随其后。
 * 两者只在 transcript 那一段错开 `transcriptWindowStart`。
 */

export type SelectionSpace = {
  /** header 高度（无 header 时为 0）。 */
  headerRows: number
  /** 整条 transcript 的**总**行数（不是视口行数）。 */
  transcriptTotalRows: number
  /** transcript 视口能显示的行数。 */
  transcriptViewportRows: number
  /** 当前滚动偏移（0 = 贴底/最新）。 */
  transcriptScrollOffset: number
  /** transcript 与 composer 之间的行数（band + 空白填充）。 */
  bandRows: number
}

function geometry(space: SelectionSpace) {
  const h = Math.max(0, Math.floor(space.headerRows))
  const v = Math.max(1, Math.floor(space.transcriptViewportRows))
  const total = Math.max(0, Math.floor(space.transcriptTotalRows))
  const offset = Math.max(0, Math.floor(space.transcriptScrollOffset))
  return { h, v, total, offset }
}

/**
 * 视口顶部对应的**内容行**。
 *
 * 与 `visibleTranscriptLines` 一致：`end = total - offset`，`start = end - v`。
 * 内容不足一屏时贴顶（start = 0）。
 */
export function transcriptWindowStart(space: SelectionSpace): number {
  const { v, total, offset } = geometry(space)
  const end = Math.max(0, total - offset)
  return Math.max(0, end - v)
}

/** transcript 视口里真正有内容的行数（内容不足一屏时小于 `v`）。 */
export function visibleTranscriptContentRows(space: SelectionSpace): number {
  const { v, total } = geometry(space)
  return Math.min(total, v)
}

/**
 * 屏幕行 → 内容行。屏幕上没有内容对应的行（视口里的空白填充）返回 null。
 */
export function screenRowToContentRow(
  space: SelectionSpace,
  screenRow: number,
): number | null {
  const { h, v, total } = geometry(space)
  const s = Math.floor(screenRow)
  if (s < 0) return null
  if (s < h) return s
  if (s < h + v) {
    const within = s - h
    if (within >= Math.min(total, v)) return null
    return transcriptWindowStart(space) + within
  }
  return h + total + (s - h - v)
}

/** 内容行 → 屏幕行。内容在屏幕外（被滚走）时返回 null。 */
export function contentRowToScreenRow(
  space: SelectionSpace,
  contentRow: number,
): number | null {
  const { h, v, total } = geometry(space)
  const c = Math.floor(contentRow)
  if (c < 0) return null
  if (c < h) return c
  if (c < h + total) {
    const screen = h + (c - transcriptWindowStart(space))
    return screen >= h && screen < h + v ? screen : null
  }
  return c - total + v
}

export type AutoScrollDirection = 'up' | 'down' | null

/**
 * 拖到 transcript 视口之外时该往哪边滚。
 *
 * `'up'` = 往历史方向（offset 增大），`'down'` = 往最新方向。
 * 只有内容确实超出视口时才给方向 —— 不能滚的时候返回 null，
 * 否则会把焦点挪到一个不存在的行上。
 */
export function autoScrollDirection(
  space: SelectionSpace,
  screenRow: number,
): AutoScrollDirection {
  const { h, v, total } = geometry(space)
  if (total <= v) return null
  const s = Math.floor(screenRow)
  if (s < h) return 'up'
  if (s >= h + v) return 'down'
  return null
}

/**
 * 滚动到边缘后焦点应该落在的内容行。
 *
 * 拖到视口上方时焦点应是**滚完之后**的窗口首行，否则每次滚动都慢一行。
 */
export function focusContentRowAtEdge(
  spaceAfterScroll: SelectionSpace,
  direction: Exclude<AutoScrollDirection, null>,
): number {
  const { v, total } = geometry(spaceAfterScroll)
  const start = transcriptWindowStart(spaceAfterScroll)
  const visible = Math.min(total, v)
  if (visible <= 0) return start
  return direction === 'up' ? start : start + visible - 1
}

export type RowPoint = { row: number; col: number }
export type RowRange = { start: RowPoint; end: RowPoint }

/** 一个足够大的列号：表示「到本行末尾」，由下游按行宽归一。 */
const TO_ROW_END = 1e9

/**
 * 把**内容坐标**的选区投影到**屏幕坐标**，用于画高亮。
 *
 * 投影只影响渲染。选区本身仍存在内容坐标里，所以：
 * - 滚动不会让 anchor 指到别的文本；
 * - 屏幕外的部分不画（那里本来也没像素），但复制时仍能取到。
 *
 * 被滚到窗口**上方**的部分收拢到视口首行（列 0）；
 * 滚到**下方**的部分收拢到视口末行（列到行尾）——
 * 这正是 `clampPoint` 之后的既有语义，只是先按内容判断边界。
 */
export function projectRangeToScreen(
  space: SelectionSpace,
  range: RowRange,
): RowRange {
  const { h, v, total } = geometry(space)
  const winStart = transcriptWindowStart(space)
  const visible = Math.min(total, v)
  const winEnd = winStart + visible - 1

  const map = (p: RowPoint, isStart: boolean): RowPoint => {
    const c = Math.floor(p.row)
    if (c < h) return { row: c, col: p.col }
    if (c >= h + total) {
      // transcript 之后（band / composer）：整体上移 total - v 行
      return { row: c - total + v, col: p.col }
    }
    const within = c - h // 内容内的 transcript 行号
    if (within < winStart) {
      return { row: h, col: isStart ? 0 : TO_ROW_END }
    }
    if (within > winEnd) {
      return { row: h + v - 1, col: TO_ROW_END }
    }
    return { row: h + (within - winStart), col: p.col }
  }

  return { start: map(range.start, true), end: map(range.end, false) }
}
