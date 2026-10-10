/**
 * 最小「reflow 感知」虚拟终端 —— 把真机现象「终端放大/缩小后 GA Ink UI 出现重复信息」
 * 降维成确定性的字节级断言（playbook 手段一：虚拟终端追踪器）。
 *
 * 真机机制：ink 的 log-update 每帧写「eraseLines(previousLineCount) + output」，用**相对**
 * 光标移动往上擦掉上一帧。previousLineCount 是改尺寸**之前**那一帧的行数；而真实终端在
 * 宽度变化时会 reflow（重排缓冲区：长行重新折行）。上一帧在屏幕上实际占用的行数因此变了，
 * 相对擦除擦不干净 → 旧帧顶行残留 → 视觉上「重复信息」逐次叠加。
 *
 * 这个模型把「终端 reflow」变成可解释、可断言的确定性行为：
 * - 缓冲区保存**逻辑行**（写入的文本），可视行由当前 columns **派生**折行；
 *   因此 columns 一改，reflow 自动发生，无需额外模拟；
 * - 光标锚在 (逻辑行, 行内偏移)，与真实终端一致（reflow 时光标跟着文本走）；
 * - 解释 ink / GA 实际写出的 CSI：eraseLines 的 erase-line/up 组合、CSI n A/B/C/D、
 *   CSI n G、CSI r;c H、CSI 2K、CSI 2J、CSI 3J、CSI ?25h/l、CR、LF。
 *
 * 保真度边界（诚实标注）：按「1 字符 = 1 列」折行，不模拟宽字符/组合字符的终端宽度表，
 * 也不模拟 DECSTBM / 备用屏 / 滚动区域。测试只用 ASCII 探针，避免把宽度表差异混进断言。
 * 被测代码引入新的控制序列时，这里必须同步支持（否则测试会假绿或误红）。
 */

const ESC = String.fromCharCode(27)
const CR = String.fromCharCode(13)
const LF = String.fromCharCode(10)
const PARAM_CHARS = '0123456789;?'

type Cursor = { line: number; offset: number }

export class ReflowTerminal {
  columns: number
  rows: number
  private lines: string[] = ['']
  private cursor: Cursor = { line: 0, offset: 0 }

  constructor(columns: number, rows: number) {
    this.columns = Math.max(1, Math.floor(columns))
    this.rows = Math.max(1, Math.floor(rows))
  }

  /** 当前 columns 下，整个缓冲区（含滚出屏幕的历史）折行后的可视行。 */
  visualRows(): string[] {
    const out: string[] = []
    for (const line of this.lines) out.push(...this.wrap(line))
    return out
  }

  /** 屏幕上的 rows 行（底对齐，不足则上方补空行）；行尾空白裁掉。 */
  screen(): string[] {
    const all = this.visualRows()
    const tail = all.slice(Math.max(0, all.length - this.rows))
    const pad = Array.from({ length: Math.max(0, this.rows - tail.length) }, () => '')
    return [...pad, ...tail].map(line => line.trimEnd())
  }

  /** 光标所在可视行（0 起，含已滚出屏幕的历史行）。 */
  cursorVisualRow(): number {
    return this.visualStartOf(this.cursor.line) + this.rowInLineOfCursor()
  }

  write(input: string): void {
    let i = 0
    while (i < input.length) {
      const ch = input[i]
      if (ch === undefined) break
      if (ch === ESC) {
        const next = this.readCsi(input, i)
        if (next !== null) {
          i = next
          continue
        }
        i += 2
        continue
      }
      if (ch === CR) {
        this.cursor.offset = this.rowStartOfCursor()
        i += 1
        continue
      }
      if (ch === LF) {
        this.lineFeed()
        i += 1
        continue
      }
      this.putChar(ch)
      i += 1
    }
  }

  /** 解析 ESC [ params final，返回消费到的下标；不是 CSI 时返回 null。 */
  private readCsi(input: string, start: number): number | null {
    if (input[start + 1] !== '[') return null
    let j = start + 2
    let params = ''
    while (j < input.length && PARAM_CHARS.includes(input[j] ?? '')) {
      params += input[j]
      j += 1
    }
    const final = input[j]
    if (final === undefined || !/[A-Za-z]/.test(final)) return null
    this.applyCsi(params, final)
    return j + 1
  }

  private wrap(line: string): string[] {
    if (line.length === 0) return ['']
    const out: string[] = []
    for (let i = 0; i < line.length; i += this.columns) out.push(line.slice(i, i + this.columns))
    return out
  }

  private visualStartOf(lineIndex: number): number {
    let start = 0
    for (let i = 0; i < lineIndex; i += 1) start += this.wrap(this.lines[i] ?? '').length
    return start
  }

  private rowInLineOfCursor(): number {
    const rows = this.wrap(this.lines[this.cursor.line] ?? '')
    return Math.min(Math.floor(this.cursor.offset / this.columns), rows.length - 1)
  }

  private rowStartOfCursor(): number {
    return this.rowInLineOfCursor() * this.columns
  }

  private lineOfVisualRow(row: number): { line: number; rowInLine: number } {
    let remaining = Math.max(0, row)
    for (let i = 0; i < this.lines.length; i += 1) {
      const count = this.wrap(this.lines[i] ?? '').length
      if (remaining < count) return { line: i, rowInLine: remaining }
      remaining -= count
    }
    const last = this.lines.length - 1
    return { line: last, rowInLine: this.wrap(this.lines[last] ?? '').length - 1 }
  }

  /** 把光标移到可视行 row / 行内列 col（终端里的绝对定位，越界自动钳制）。 */
  private setVisual(row: number, col: number): void {
    const total = this.visualRows().length
    const clampedRow = Math.max(0, Math.min(row, total - 1))
    const { line, rowInLine } = this.lineOfVisualRow(clampedRow)
    const base = rowInLine * this.columns
    const length = (this.lines[line] ?? '').length
    const limit = Math.min(Math.max(base, length), base + this.columns)
    this.cursor = { line, offset: Math.min(base + Math.max(0, col), Math.max(base, limit)) }
  }

  private putChar(ch: string): void {
    const line = this.lines[this.cursor.line] ?? ''
    if (this.cursor.offset > line.length) {
      this.lines[this.cursor.line] = line + ' '.repeat(this.cursor.offset - line.length)
    }
    const current = this.lines[this.cursor.line] ?? ''
    this.lines[this.cursor.line] = current.slice(0, this.cursor.offset) + ch + current.slice(this.cursor.offset + 1)
    this.cursor.offset += 1
  }

  private lineFeed(): void {
    const row = this.cursorVisualRow()
    const total = this.visualRows().length
    const lastLine = this.lines.length - 1
    if (row + 1 < total) {
      this.setVisual(row + 1, 0)
      return
    }
    if ((this.lines[lastLine] ?? '') === '') {
      this.cursor = { line: lastLine, offset: 0 }
      return
    }
    this.lines.push('')
    this.cursor = { line: this.lines.length - 1, offset: 0 }
  }

  private eraseCurrentRow(): void {
    const base = this.rowStartOfCursor()
    const line = this.lines[this.cursor.line] ?? ''
    const end = Math.min(line.length, base + this.columns)
    if (end <= base) return
    this.lines[this.cursor.line] = line.slice(0, base) + ' '.repeat(end - base) + line.slice(end)
  }

  private eraseToRowEnd(): void {
    const line = this.lines[this.cursor.line] ?? ''
    this.lines[this.cursor.line] = line.slice(0, this.cursor.offset)
  }

  private applyCsi(rawParams: string, final: string): void {
    if (rawParams.startsWith('?')) return // DEC private mode（?25h / ?25l）：与画面无关
    const values = rawParams.split(';').map(part => (part === '' ? Number.NaN : Number(part)))
    const arg = (index: number, fallback: number): number => {
      const value = values[index]
      return value !== undefined && Number.isFinite(value) ? Number(value) : fallback
    }
    const visualRow = this.cursorVisualRow()
    const colInRow = this.cursor.offset - this.rowStartOfCursor()
    switch (final) {
      case 'A':
        this.setVisual(visualRow - arg(0, 1), colInRow)
        return
      case 'B':
        this.setVisual(visualRow + arg(0, 1), colInRow)
        return
      case 'C':
        this.cursor.offset += arg(0, 1)
        return
      case 'D':
        this.cursor.offset = Math.max(this.rowStartOfCursor(), this.cursor.offset - arg(0, 1))
        return
      case 'G':
        this.setVisual(visualRow, arg(0, 1) - 1)
        return
      case 'H':
      case 'f':
        this.setVisual(arg(0, 1) - 1, arg(1, 1) - 1)
        return
      case 'K':
        if (arg(0, 0) === 0) this.eraseToRowEnd()
        else this.eraseCurrentRow()
        return
      case 'J': {
        const mode = arg(0, 0)
        if (mode === 2) {
          // 整屏清除。模型只保留可视内容，所以「清屏 + 清 scrollback + 归位」= 缓冲区归零。
          this.lines = ['']
          this.cursor = { line: 0, offset: 0 }
          return
        }
        if (mode === 3) return // 清 scrollback：模型不保留屏幕之外的历史
        this.eraseToRowEnd()
        this.lines = this.lines.slice(0, this.cursor.line + 1)
        return
      }
      default:
        return
    }
  }
}

/** 统计屏幕上包含 needle 的行数（重复渲染 = 计数 > 1）。 */
export function countVisibleRows(screen: string[], needle: string): number {
  return screen.filter(line => line.includes(needle)).length
}
