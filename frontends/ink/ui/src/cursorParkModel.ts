/**
 * 路径 A 光标协调模型 —— 纯函数 + 确定性光标模拟，用于验证与回归。
 *
 * 背景：ink@5 的 log-update 每帧写 `eraseLines(prevN) + output`，其中
 * `eraseLines(N) = \x1b[2K(\x1b[1A\x1b[2K)*\x1b[G` —— 纯相对移动，从光标
 * **当前位置**往上擦 N 行。ink 假设每帧写完光标停在内容底部下一行，下一帧
 * 从那里往上擦。
 *
 * GA 现状 bug：App 在 useLayoutEffect 里旁路写 `\x1b[s` + 绝对 CUP 把光标挪到
 * 输入框中部的 caret。ink 的 throttledLog 与该 effect 不同步：若 ink 在下一帧
 * 写入时光标仍停在 caret（中部，未复位），eraseLines 就从中部往上擦 → 擦错行
 * → ghost composer / 整框漂移。
 *
 * 本模型用一个解释 ANSI 的虚拟光标追踪器，确定性地判定 eraseLines 是否擦到了
 * 上一帧内容的行区间。bug 是相对光标算术，完全由转义序列决定，无需真实终端。
 */

export type Cursor = { row: number; col: number }

/**
 * 极简虚拟终端光标追踪器。追踪光标 (row,col) 与被 `\x1b[K` 擦除的行。
 * row/col 0-based；允许 row 越界为负（表示擦到了内容顶行之上，即擦错位置）。
 */
export class CursorTracker {
  cursor: Cursor = { row: 0, col: 0 }
  saved: Cursor | null = null
  erasedRows: number[] = []
  maxRowWritten = 0

  feed(seq: string): void {
    let i = 0
    while (i < seq.length) {
      const ch = seq[i]!
      if (ch === '\x1b' && seq[i + 1] === '[') {
        let j = i + 2
        // DEC 私有模式（如 ESC[?25h / ESC[?25l 显隐光标）：`?` 前缀，参数后跟 h/l。
        // 追踪器不关心可见性，只需整段吞掉，别把 `25h` 当成内容写进屏。
        if (seq[j] === '?') {
          j += 1
          while (j < seq.length && /[0-9;]/.test(seq[j]!)) j += 1
          i = j + 1 // 跳过终结符 h/l
          continue
        }
        let params = ''
        while (j < seq.length && /[0-9;]/.test(seq[j]!)) {
          params += seq[j]!
          j += 1
        }
        const final = seq[j]!
        const n = params === '' ? 1 : parseInt(params.split(';')[0]!, 10)
        switch (final) {
          case 'A': this.cursor.row -= n; break
          case 'B': this.cursor.row += n; break
          case 'C': this.cursor.col += n; break
          case 'D': this.cursor.col = Math.max(0, this.cursor.col - n); break
          case 'G': this.cursor.col = Math.max(0, (params === '' ? 1 : n) - 1); break
          case 'H': {
            const parts = params.split(';')
            const r = parts[0] ? parseInt(parts[0], 10) : 1
            const c = parts[1] ? parseInt(parts[1], 10) : 1
            this.cursor.row = r - 1
            this.cursor.col = c - 1
            break
          }
          case 'K': this.erasedRows.push(this.cursor.row); break
          case 's': this.saved = { ...this.cursor }; break
          case 'u': if (this.saved) this.cursor = { ...this.saved }; break
          default: break
        }
        i = j + 1
        continue
      }
      if (ch === '\n') {
        this.cursor.row += 1
        this.maxRowWritten = Math.max(this.maxRowWritten, this.cursor.row)
        i += 1
        continue
      }
      if (ch === '\r') {
        this.cursor.col = 0
        i += 1
        continue
      }
      this.cursor.col += 1
      this.maxRowWritten = Math.max(this.maxRowWritten, this.cursor.row)
      i += 1
    }
  }
}

/** ink@5 eraseLines(count)：实测自 ansi-escapes。 */
export function eraseLines(count: number): string {
  let clear = ''
  for (let i = 0; i < count; i++) {
    clear += '\x1b[2K' + (i < count - 1 ? '\x1b[1A' : '')
  }
  if (count) clear += '\x1b[G'
  return clear
}

/** ink 写一帧：eraseLines(prevLineCount) + output（output = body + '\n'）。 */
export function inkFrame(body: string, prevLineCount: number): { seq: string; lineCount: number } {
  const output = body + '\n'
  return { seq: eraseLines(prevLineCount) + output, lineCount: output.split('\n').length }
}

export type ParkApproach =
  | 'none'            // 不放 caret：光标留在底部（ink 理想，但 IME 不贴 caret）
  | 'current-sco'     // 现状：帧末 \x1b[s + 绝对 CUP 到 caret，下一帧 ink 写入前未复位（竞态）
  | 'wrapper-relative' // 路径 A：帧末相对上移到 caret；下一帧开头相对下移复位到底部

export type SimulationResult = { ok: boolean; detail: string[] }

/**
 * 模拟连续 frames 帧渲染。每帧内容 body 固定，caret 在 caretRowInFrame 行。
 * 不变量：每帧 ink 的 eraseLines 必须覆盖上一帧内容的顶行（否则擦错 → ghost/漂移）。
 */
export function simulateFrames(
  approach: ParkApproach,
  frames: number,
  body: string[],
  caretRowInFrame: number,
  caretCol: number,
): SimulationResult {
  const t = new CursorTracker()
  const detail: string[] = []
  let prevLineCount = 0
  let prevFrameTopRow = 0

  for (let f = 0; f < frames; f++) {
    // 帧开始：路径 A 的包装器先把光标从 caret 相对复位到底部（ink 期望起点）。
    if (approach === 'wrapper-relative' && f > 0) {
      const rowsFromCaretToBottom = body.length - caretRowInFrame
      t.feed(`\x1b[${rowsFromCaretToBottom}B\x1b[G`)
    }
    // current-sco：模拟竞态——ink 写入时光标仍停在上一帧末尾放置的 caret（未复位）。

    const frameTopRow = t.cursor.row
    const { seq, lineCount } = inkFrame(body.join('\n'), prevLineCount)
    t.erasedRows = []
    const eraseStartRow = t.cursor.row
    t.feed(seq)

    const bottomRow = t.cursor.row
    const caretAbsRow = bottomRow - (body.length - caretRowInFrame)
    if (approach === 'current-sco') {
      t.feed('\x1b[s')
      t.feed(`\x1b[${caretAbsRow + 1};${caretCol + 1}H`)
    } else if (approach === 'wrapper-relative') {
      const up = bottomRow - caretAbsRow
      t.feed(`\x1b[${up}A\x1b[${caretCol + 1}G`)
    }

    if (f > 0) {
      const erasedTop = Math.min(...t.erasedRows)
      const erasedBottom = Math.max(...t.erasedRows)
      const covered = erasedTop <= prevFrameTopRow && erasedBottom >= prevFrameTopRow
      detail.push(
        `frame ${f}: eraseStart=${eraseStartRow} erased[${erasedTop}..${erasedBottom}] prevContentTop=${prevFrameTopRow} covered=${covered}`,
      )
      if (!covered) return { ok: false, detail }
    } else {
      detail.push(`frame ${f}: (baseline) top=${frameTopRow}`)
    }

    prevLineCount = lineCount
    prevFrameTopRow = frameTopRow
  }

  return { ok: true, detail }
}
