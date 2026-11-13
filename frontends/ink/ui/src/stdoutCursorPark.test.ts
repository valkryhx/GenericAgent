import test from 'node:test'
import assert from 'node:assert/strict'
import { CursorParkWriter } from './stdoutCursorPark.js'
import { CursorTracker } from './cursorParkModel.js'
import { BSU, ESU } from './syncOutput.js'
import { applyToScreen, createScreen } from './virtualScreen.js'

// 手动调度器：把 microtask 收集起来，测试里显式 flush，模拟「一轮同步写入后触发」。
function manualScheduler() {
  const queue: Array<() => void> = []
  const schedule = (cb: () => void) => { queue.push(cb) }
  const flush = () => {
    const pending = queue.splice(0)
    for (const cb of pending) cb()
  }
  return { schedule, flush }
}

/** 关掉同步输出的 writer（回退路径 / 旧行为）。 */
function plainWriter(out: string[], schedule: (cb: () => void) => void) {
  return new CursorParkWriter((c) => out.push(c), schedule, { syncOutput: false })
}

/** 开同步输出的 writer（新默认路径）。 */
function syncWriter(out: string[], schedule: (cb: () => void) => void) {
  return new CursorParkWriter((c) => out.push(c), schedule, { syncOutput: true })
}

test('no park spec: write passes through unchanged, cursor never parked', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = plainWriter(out, schedule)
  w.write('frame-a')
  flush()
  assert.deepEqual(out, ['frame-a'])
  assert.equal(w.parkedUp, null)
})

test('one onRender burst becomes exactly ONE write, with park appended', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = plainWriter(out, schedule)
  w.setPark({ up: 2, col: 5 })
  // ink 一轮 onRender 的 3 次同步写入（clear / static / 主帧）必须合并。
  w.write('clear')
  w.write('static')
  w.write('main-frame')
  assert.equal(out.length, 0, 'nothing may reach the terminal before the burst settles')
  flush()
  // 合并成一次 write：内容保持顺序，末尾是 park（上移 2 行 + 第 6 列 + SHOW）。
  assert.deepEqual(out, ['clearstaticmain-frame\x1b[2A\x1b[6G\x1b[?25h'])
  assert.equal(w.parkedUp, 2)
})

test('unpark before next write returns cursor to frame bottom (relative down + CR)', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = plainWriter(out, schedule)
  w.setPark({ up: 3, col: 4 })
  w.write('frame-1')
  flush()                       // park: up 3
  assert.equal(w.parkedUp, 3)
  w.write('frame-2')            // 写入前应先 unpark：HIDE + 下移 3 + CR
  flush()
  assert.equal(out.length, 2)
  assert.equal(out[1], '\x1b[?25l\x1b[3B\rframe-2\x1b[3A\x1b[5G\x1b[?25h')
  assert.equal(w.parkedUp, 3)
})

test('up=0 park (caret on frame-bottom row) uses bare CR to unpark', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = plainWriter(out, schedule)
  w.setPark({ up: 0, col: 7 })
  w.write('f1')
  flush()
  assert.equal(out[0], 'f1\x1b[8G\x1b[?25h')   // 无上移，仅定列 + SHOW
  w.write('f2')
  flush()
  assert.equal(out[1], '\x1b[?25l\rf2\x1b[8G\x1b[?25h')
})

test('dispose stops parking and drops buffered bytes', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = plainWriter(out, schedule)
  w.setPark({ up: 1, col: 2 })
  w.write('f1')
  w.dispose()
  flush()                       // microtask 触发时已 disposed → 不写出
  assert.deepEqual(out, [])
  assert.equal(w.parkedUp, null)
})

// ── 同步输出路径（新默认）────────────────────────────────────────────────

test('sync output: the whole burst is bracketed by exactly one BSU/ESU pair', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = syncWriter(out, schedule)
  w.setPark({ up: 2, col: 5 })
  w.write('clear')
  w.write('main-frame')
  flush()

  assert.equal(out.length, 1, 'a burst must produce a single atomic write')
  const chunk = out[0]!
  assert.ok(chunk.startsWith(BSU), 'must begin with BSU')
  assert.ok(chunk.endsWith(ESU), 'must end with ESU')
  // 配对完整：各出现一次，否则终端会一直停在「同步中」。
  assert.equal(chunk.split(BSU).length - 1, 1)
  assert.equal(chunk.split(ESU).length - 1, 1)
  // 内容与 park 都在事务内。
  assert.ok(chunk.includes('clearmain-frame'))
  assert.ok(chunk.includes('\x1b[2A\x1b[6G'))
})

test('sync output: no per-frame HIDE/SHOW flip while the cursor stays on the caret', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = syncWriter(out, schedule)
  w.setPark({ up: 4, col: 3 })

  // 连续 5 帧：光标始终停在 caret，中间态不可见，不该发生任何 DECTCEM 翻转。
  for (let f = 0; f < 5; f += 1) {
    w.write(`frame-${f}`)
    flush()
  }

  const all = out.join('')
  const hides = all.split('\x1b[?25l').length - 1
  const shows = all.split('\x1b[?25h').length - 1
  // 只在第一次从「不可见」变「可见」时 SHOW 一次，此后不再翻转。
  assert.equal(hides, 0, `unexpected HIDE while parked on the caret (${hides})`)
  assert.equal(shows, 1, `expected exactly one SHOW, got ${shows}`)
})

test('sync output: HIDE is still emitted when the frame ends un-parked', () => {
  // 面板打开 / 未就绪 → setPark(null)：光标最终停在帧底且可见，必须遮蔽。
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = syncWriter(out, schedule)
  w.setPark({ up: 4, col: 3 })
  w.write('frame-1')
  flush()
  assert.equal(w.cursorShown, true)

  w.setPark(null)
  w.write('frame-2')
  flush()

  const all = out.join('')
  // 本帧不 park → 必须发 HIDE，否则可见光标停在帧底，且 IME 会锚到错误的位置。
  assert.equal(all.split('\x1b[?25l').length - 1, 1)
  assert.equal(w.cursorShown, false)
  assert.equal(w.parkedUp, null)
})

test('sync output: the cursor is always visible whenever it is parked', () => {
  // IME 锚定的不变量：parkedUp !== null ⟹ cursorShown === true。
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = syncWriter(out, schedule)
  w.setPark({ up: 3, col: 2 })
  for (let f = 0; f < 4; f += 1) {
    w.write(`f${f}`)
    flush()
    assert.equal(w.parkedUp !== null, w.cursorShown, `frame ${f}: visibility must mirror parking`)
  }
})

test('resetViewport writes the full reset and drops park bookkeeping', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = plainWriter(out, schedule)
  w.setPark({ up: 2, col: 3 })
  w.write('frame')
  flush()
  assert.equal(w.parkedUp, 2)

  const reset = '\x1b[2J\x1b[3J\x1b[H'
  w.resetViewport(reset)
  assert.deepEqual(out.slice(-1), [reset])
  // 光标已被 CSI H 归位到屏幕左上：parkedUp 必须清空，下一次写入不能再按旧几何下移。
  assert.equal(w.parkedUp, null)
  w.write('frame-2')
  flush()
  assert.ok(out.at(-1)!.endsWith('frame-2\x1b[2A\x1b[4G'), 'no stale down-move may precede the new frame')
})

test('resetViewport invalidates an already-scheduled flush', () => {
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = plainWriter(out, schedule)
  w.setPark({ up: 2, col: 3 })
  w.write('frame')
  w.resetViewport('RESET')
  flush()
  assert.deepEqual(out, ['RESET'])
  assert.equal(w.parkedUp, null)
})

// 端到端不变量：用虚拟终端追踪器验证连续多帧下 eraseLines 始终从帧底往上擦。
test('end-to-end: eraseLines always erases from frame bottom (virtual terminal)', () => {
  const BODY = ['hint', '────────', '> caret']  // 3 行，caret 在末行
  const caretUpFromBottom = 1                    // 帧底(内容后一行) → caret 行 上移 1
  const caretCol = 2
  const { schedule, flush } = manualScheduler()
  const t = new CursorTracker()
  // 关掉行级 diff：这条测的是「全量重写路径」的几何不变量。
  const w = new CursorParkWriter((c) => t.feed(c), schedule, { syncOutput: false, lineDiff: false })

  const eraseLines = (n: number) => {
    let s = ''
    for (let i = 0; i < n; i++) s += '\x1b[2K' + (i < n - 1 ? '\x1b[1A' : '')
    if (n) s += '\x1b[G'
    return s
  }

  let prevLineCount = 0
  let prevContentTop = 0
  for (let f = 0; f < 6; f++) {
    w.setPark({ up: caretUpFromBottom, col: caretCol })
    const contentTop = t.cursor.row
    t.erasedRows = []
    const output = BODY.join('\n') + '\n'
    w.write(eraseLines(prevLineCount) + output)   // ink 一帧
    flush()
    if (f > 0) {
      const erasedTop = Math.min(...t.erasedRows)
      const erasedBottom = Math.max(...t.erasedRows)
      assert.ok(
        erasedTop <= prevContentTop && erasedBottom >= prevContentTop,
        `frame ${f}: erased[${erasedTop}..${erasedBottom}] must cover prevContentTop=${prevContentTop}`,
      )
    }
    prevLineCount = output.split('\n').length
    prevContentTop = contentTop
  }
})

// ── 行级 diff 接入后的端到端一致性 ──────────────────────────────────────

/** ink 的 eraseLines(N)。 */
function eraseLines(count: number): string {
  let s = ''
  for (let i = 0; i < count; i++) s += '\u001b[2K' + (i < count - 1 ? '\u001b[1A' : '')
  if (count) s += '\u001b[G'
  return s
}

/**
 * 连续帧序列：屏幕内容与光标必须与「每帧全量重写」完全一致。
 *
 * 这是行级 diff 接入 writer 后的核心回归 —— 单测 `lineDiff.test.ts` 只验纯函数，
 * 这里验的是它被 writer 正确调用（记账、首帧、变矮退化都走对了分支）。
 */
test('end-to-end: line diff produces the same screen as full rewrite', () => {
  const frames: string[][] = [
    ['header', 'body 1', '> '],
    ['header', 'body 1', '> a'],
    ['header', 'body 1', '> ab'],
    ['header', 'body 1', '> a'],
    ['header', 'body 1', 'streaming', '> a'],
    ['header', 'body 1', 'streaming', 'more', '> a'],
    ['header', 'body 1', 'streaming', 'more', '> '],
  ]

  // A 组：全量重写（lineDiff=false）；B 组：行级 diff（lineDiff=true）。
  const { schedule: schA, flush: flushA } = manualScheduler()
  const { schedule: schB, flush: flushB } = manualScheduler()
  const screenA = createScreen(40, 40)
  const screenB = createScreen(40, 40)
  const wA = new CursorParkWriter((c) => applyToScreen(screenA, c, 40), schA, { syncOutput: false, lineDiff: false })
  const wB = new CursorParkWriter((c) => applyToScreen(screenB, c, 40), schB, { syncOutput: false, lineDiff: true })

  let prevCount = 0
  for (const body of frames) {
    for (const w of [wA, wB]) {
      w.setPark({ up: 1, col: 2 })
      // 每帧都从帧底擦 prevCount 行再重写 —— 这是 ink 的真实输出形态。
      w.write(eraseLines(prevCount) + body.join('\n') + '\n')
    }
    flushA()
    flushB()
    prevCount = body.length + 1

    assert.equal(screenA.row, screenB.row, `cursor row after ${JSON.stringify(body)}`)
    assert.equal(screenA.col, screenB.col, `cursor col after ${JSON.stringify(body)}`)
    for (let r = 0; r < Math.max(screenA.rows.length, screenB.rows.length); r += 1) {
      assert.equal(
        (screenA.rows[r] ?? '').trimEnd(),
        (screenB.rows[r] ?? '').trimEnd(),
        `row ${r} after ${JSON.stringify(body)}`,
      )
    }
  }
})

test('end-to-end: line diff falls back when the frame shrinks', () => {
  const { schedule, flush } = manualScheduler()
  const screen = createScreen(40, 40)
  const w = new CursorParkWriter((c) => applyToScreen(screen, c, 40), schedule, { syncOutput: false, lineDiff: true })

  // 首帧
  w.setPark({ up: 1, col: 2 })
  w.write(eraseLines(0) + 'a\nb\nc\n')
  flush()

  // 变矮：3 行 → 1 行。diff 必须放弃，退化为原样透传。
  w.setPark({ up: 1, col: 2 })
  const shrinkFrame = eraseLines(4) + 'a\n'
  w.write(shrinkFrame)
  flush()

  // 屏幕上只剩 a（b/c 已被擦除），且不能残留。
  const visible = screen.rows.map(l => l.trimEnd()).filter(Boolean)
  assert.deepEqual(visible, ['a'])
})

test('line diff is skipped when the burst contains more than one frame', () => {
  // log.clear() + <Static> + 主帧：一次突发里两段擦除，帧锚点已变 → 必须原样透传。
  const out: string[] = []
  const { schedule, flush } = manualScheduler()
  const w = new CursorParkWriter((c) => out.push(c), schedule, { syncOutput: false, lineDiff: true })
  w.setPark({ up: 2, col: 3 })
  const burst = `${eraseLines(2)}static-line\n${eraseLines(2)}main\nbody\n`
  w.write(burst)
  flush()
  // 原样透传：整段 burst 一字不改地出现在输出里。
  assert.ok(out[0]!.includes(burst), 'a multi-frame burst must pass through verbatim')
})
